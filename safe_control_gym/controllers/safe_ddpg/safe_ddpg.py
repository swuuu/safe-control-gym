"""Deep Deterministic Policy Gradient

Reference paper & code:
    * [Continuous Control with Deep Reinforcement Learning](https://arxiv.org/pdf/1509.02971.pdf)
    * [openai spinning up - ddpg](https://github.com/openai/spinningup/tree/master/spinup/algos/pytorch/ddpg)
    * [DeepRL - ddpg](https://github.com/ShangtongZhang/DeepRL/blob/master/deep_rl/agent/DDPG_agent.py)

Example:
    $ python experiments/main.py --algo ddpg --task cartpole --output_dir results --tag test/cartpole_ddpg --seed 6

Todo
    *

"""
import os
import time
import copy
import numpy as np
import torch
from collections import defaultdict

from safe_control_gym.utils.logging import ExperimentLogger
from safe_control_gym.utils.utils import get_random_state, set_random_state, is_wrapped
from safe_control_gym.envs.env_wrappers.vectorized_env import make_vec_envs
from safe_control_gym.envs.env_wrappers.vectorized_env.vec_env_utils import _flatten_obs, _unflatten_obs
from safe_control_gym.envs.env_wrappers.record_episode_statistics import RecordEpisodeStatistics, \
    VecRecordEpisodeStatistics
from safe_control_gym.math_and_models.normalization import BaseNormalizer, MeanStdNormalizer, RewardStdNormalizer

from safe_control_gym.controllers.base_controller import BaseController
from safe_control_gym.controllers.safe_ddpg.safe_explorer_utils import SafetyLayer, ConstraintBuffer
from safe_control_gym.controllers.safe_ddpg.safe_ddpg_utils import SafeDDPGAgent, SafeDDPGBuffer, make_action_noise_process


class SafeExplorerDDPG(BaseController):
    """deep deterministic policy gradient."""

    def __init__(self,
                 env_func,
                 training=True,
                 checkpoint_path="model_latest.pt",
                 output_dir="temp",
                 use_gpu=False,
                 seed=0,
                 **kwargs):
        super().__init__(env_func, training, checkpoint_path, output_dir, use_gpu, seed, **kwargs)

        # task
        if self.training:
            # training (+ evaluation)
            self.env = make_vec_envs(env_func, None, self.rollout_batch_size, self.num_workers, seed)
            self.env = VecRecordEpisodeStatistics(self.env, self.deque_size)
            self.eval_env = env_func(seed=seed * 111)
            self.eval_env = RecordEpisodeStatistics(self.eval_env, self.deque_size)
            # action noise for training
            self.noise_process = None
            # if self.random_process:
            #     self.noise_process = make_action_noise_process(self.random_process, self.env.action_space)
            self.num_constraints = self.env.envs[0].num_constraints
        else:
            # testing only
            self.env = env_func()
            self.env = RecordEpisodeStatistics(self.env)
            self.num_constraints = self.env.num_constraints
        # Safety layer.
        self.safety_layer = SafetyLayer(self.env.observation_space,
                                        self.env.action_space,
                                        hidden_dim=self.constraint_hidden_dim,
                                        num_constraints=self.num_constraints,
                                        lr=self.constraint_lr,
                                        slack=self.constraint_slack)
        self.safety_layer.to(self.device)
        # agent
        self.agent = SafeDDPGAgent(self.env.observation_space,
                               self.env.action_space,
                               hidden_dim=self.hidden_dim,
                               gamma=self.gamma,
                               tau=self.tau,
                               actor_lr=self.actor_lr,
                               critic_lr=self.critic_lr,
                               action_modifier=self.safety_layer.get_safe_action)
        self.agent.to(self.device)

        # pre-/post-processing
        self.obs_normalizer = BaseNormalizer()
        if self.norm_obs:
            self.obs_normalizer = MeanStdNormalizer(shape=self.env.observation_space.shape, clip=self.clip_obs,
                                                    epsilon=1e-8)

        self.reward_normalizer = BaseNormalizer()
        if self.norm_reward:
            self.reward_normalizer = RewardStdNormalizer(gamma=self.gamma, clip=self.clip_reward, epsilon=1e-8)

        # logging
        if self.training:
            log_file_out = True
            use_tensorboard = self.tensorboard
        else:
            # disable logging to texts and tfboard for testing
            log_file_out = False
            use_tensorboard = False
        self.logger = ExperimentLogger(output_dir, log_file_out=log_file_out, use_tensorboard=use_tensorboard)

    def reset(self):
        """Prepares for training or testing."""
        if self.training:
            if self.pretraining:
                self.constraint_buffer = ConstraintBuffer(self.env.observation_space, self.env.action_space, self.num_constraints, self.constraint_buffer_size)
            else:
                # Load safety layer for 2nd stage training.
                assert self.pretrained, "Must provide a pre-trained model for adaptation."
                if os.path.isdir(self.pretrained):
                    self.pretrained = os.path.join(self.pretrained, "model_latest.pt")
                state = torch.load(self.pretrained)
                self.safety_layer.load_state_dict(state["safety_layer"])
                # Set up stats tracking.
                self.env.add_tracker("constraint_violation", 0)
                self.env.add_tracker("constraint_violation", 0, mode="queue")
                self.eval_env.add_tracker("constraint_violation", 0, mode="queue")
                self.eval_env.add_tracker("mse", 0, mode="queue")
            self.total_steps = 0
            obs, info = self.env.reset()
            self.obs = self.obs_normalizer(obs)
            self.c = np.array([inf["constraint_values"] for inf in info["n"]])
            self.buffer = SafeDDPGBuffer(self.env.observation_space, self.env.action_space, self.max_buffer_size,
                                     self.train_batch_size)
            # reset/initial noise process
            if self.noise_process:
                self.noise_process.reset_states()
        else:
            # set up stats tracking
            self.env.add_tracker("constraint_violation", 0, mode="queue")
            self.env.add_tracker("constraint_values", 0, mode="queue")
            self.env.add_tracker("mse", 0, mode="queue")

    def close(self):
        """Shuts down and cleans up lingering resources."""
        self.env.close()
        if self.training:
            self.eval_env.close()
        self.logger.close()

    def save(self, path, save_buffer=True):
        """Saves model params and experiment state to checkpoint path."""
        path_dir = os.path.dirname(path)
        os.makedirs(path_dir, exist_ok=True)

        state_dict = {
            "agent": self.agent.state_dict(),
            "safety_layer": self.safety_layer.state_dict(),
            "obs_normalizer": self.obs_normalizer.state_dict(),
            "reward_normalizer": self.reward_normalizer.state_dict()
        }
        if self.training:
            exp_state = {
                "total_steps": self.total_steps,
                "obs": self.obs,
                "c": self.c,
                "random_state": get_random_state(),
                "env_random_state": self.env.get_env_random_state()
            }
            # latest checkpoint shoud enable save_buffer (for experiment restore),
            # but intermediate checkpoint shoud not, to save storage (buffer is large)
            if save_buffer:
                exp_state["buffer"] = self.buffer.state_dict()
            # noise process is also stateful
            if self.noise_process:
                exp_state["noise_process"] = self.noise_process.state_dict()
            state_dict.update(exp_state)
            if self.pretraining:
                state_dict["constraint_buffer"] = self.constraint_buffer.state_dict()
        torch.save(state_dict, path)

    def load(self, path):
        """Restores model and experiment given checkpoint path."""
        state = torch.load(path)

        # restore params
        self.agent.load_state_dict(state["agent"])
        self.safety_layer.load_state_dict(state["safety_layer"])

        self.obs_normalizer.load_state_dict(state["obs_normalizer"])
        self.reward_normalizer.load_state_dict(state["reward_normalizer"])

        # restore experiment state
        if self.training:
            self.total_steps = state["total_steps"]
            self.obs = state["obs"]
            self.c = state["c"]
            set_random_state(state["random_state"])
            self.env.set_env_random_state(state["env_random_state"])
            if "buffer" in state:
                self.buffer.load_state_dict(state["buffer"])
            if self.noise_process:
                self.noise_process.load_state_dict(state["noise_process"])
            self.logger.load(self.total_steps)
            if self.pretraining:
                self.constraint_buffer.load_state_dict(state["constraint_buffer"])

    def learn(self, env=None, **kwargs):
        """Performs learning (pre-training, training, fine-tuning, etc)."""
        if self.pretraining:
            final_step = self.constraint_epochs
            train_func = self.pretrain_step
        else:
            final_step = self.max_env_steps
            train_func = self.train_step
        while self.total_steps < final_step:
            results = train_func()
            # checkpoint
            if self.total_steps >= final_step or (
                    self.save_interval and self.total_steps % self.save_interval == 0):
                # latest/final checkpoint
                self.save(self.checkpoint_path)
                self.logger.info("Checkpoint | {}".format(self.checkpoint_path))
            if self.num_checkpoints and self.total_steps % (final_step // self.num_checkpoints) == 0:
                # intermediate checkpoint
                path = os.path.join(self.output_dir, "checkpoints", "model_{}.pt".format(self.total_steps))
                self.save(path, save_buffer=False)

            # eval
            if self.eval_interval and self.total_steps % self.eval_interval == 0:
                if self.pretraining:
                    eval_results = self.eval_constraint_models()
                    results["eval"] = eval_results
                else:
                    eval_results = self.run(env=self.eval_env, n_episodes=self.eval_batch_size)
                    results["eval"] = eval_results
                    self.logger.info("Eval | ep_lengths {:.2f} +/- {:.2f} | ep_return {:.3f} +/- {:.3f}".format(
                        eval_results["ep_lengths"].mean(),
                        eval_results["ep_lengths"].std(),
                        eval_results["ep_returns"].mean(),
                        eval_results["ep_returns"].std()))
                    # save best model
                    eval_score = eval_results["ep_returns"].mean()
                    eval_best_score = getattr(self, "eval_best_score", -np.infty)
                    if self.eval_save_best and eval_best_score < eval_score:
                        self.eval_best_score = eval_score
                        self.save(os.path.join(self.output_dir, "model_best.pt"))

            # logging
            if self.log_interval and self.total_steps % self.log_interval == 0:
                self.log_step(results)

    def run(self, env=None, render=False, n_episodes=10, verbose=False, **kwargs):
        """Runs evaluation with current policy."""
        self.agent.eval()
        self.obs_normalizer.set_read_only()
        if env is None:
            env = self.env
        else:
            if not is_wrapped(env, RecordEpisodeStatistics):
                env = RecordEpisodeStatistics(env, n_episodes)
                # Add episodic stats to be tracked.
                env.add_tracker("constraint_violation", 0, mode="queue")
                env.add_tracker("constraint_values", 0, mode="queue")
                env.add_tracker("mse", 0, mode="queue")

        obs, info = env.reset()
        obs = self.obs_normalizer(obs)
        c = info["constraint_values"]

        ep_returns, ep_lengths = [], []
        frames = []

        while len(ep_returns) < n_episodes:
            with torch.no_grad():
                obs = torch.FloatTensor(obs).to(self.device)
                c = torch.FloatTensor(c).to(self.device)
                action = self.agent.ac.act(obs, c=c)

            obs, reward, done, info = env.step(action)
            if render:
                env.render()
                frames.append(env.render("rgb_array"))
            if verbose:
                print("obs {} | act {}".format(obs, action))

            if done:
                assert "episode" in info
                ep_returns.append(info["episode"]["r"])
                ep_lengths.append(info["episode"]["l"])
                obs, info = env.reset()
            obs = self.obs_normalizer(obs)
            c = info["constraint_values"]

        # collect evaluation results
        ep_lengths = np.asarray(ep_lengths)
        ep_returns = np.asarray(ep_returns)
        eval_results = {"ep_returns": ep_returns, "ep_lengths": ep_lengths}
        if len(frames) > 0:
            eval_results["frames"] = frames
        # Other episodic stats from evaluation env.
        if len(env.queued_stats) > 0:
            queued_stats = {k: np.asarray(v) for k, v in env.queued_stats.items()}
            eval_results.update(queued_stats)
        return eval_results

    def pretrain_step(self):
        """Performs a pre-trianing step.

        """
        results = defaultdict(list)
        start = time.time()
        self.safety_layer.train()
        self.obs_normalizer.unset_read_only()
        # Just sample episodes for the whole epoch.
        self.collect_constraint_data(self.constraint_steps_per_epoch)
        self.total_steps += 1
        # Do the update from memory.
        for batch in self.constraint_buffer.sampler(self.constraint_batch_size):
            res = self.safety_layer.update(batch)
            for k, v in res.items():
                results[k].append(v)
        self.constraint_buffer.reset()
        results = {k: sum(v) / len(v) for k, v in results.items()}
        results.update({"step": self.total_steps, "elapsed_time": time.time() - start})
        return results

    def train_step(self, **kwargs):
        """Performs a training step."""
        self.agent.train()
        self.obs_normalizer.unset_read_only()
        obs = self.obs
        # print('########################')
        c = self.c
        # print(f'self.c = {self.c}')
        start = time.time()

        if self.total_steps < self.warm_up_steps:
            # print(f'here 1!')
            act = np.stack([self.env.action_space.sample() for _ in range(self.rollout_batch_size)])
        else:
            # print(f'here 2!') 
            with torch.no_grad():
                act = self.agent.ac.act(torch.FloatTensor(obs).to(self.device), 
                                        c=torch.FloatTensor(c).to(self.device))
                # apply action noise if specified in training config
                if self.noise_process:
                    noise = np.stack([self.noise_process.sample() for _ in range(self.rollout_batch_size)])
                    act += noise
        # print(f'act = {act}')
        next_obs, rew, done, info = self.env.step(act)

        next_obs = self.obs_normalizer(next_obs)
        rew = self.reward_normalizer(rew, done)
        mask = 1 - np.asarray(done)

        # time truncation is not true termination
        terminal_idx, terminal_obs = [], []
        for idx, inf in enumerate(info["n"]):
            if "terminal_info" not in inf:
                continue
            inff = inf["terminal_info"]
            if "TimeLimit.truncated" in inff and inff["TimeLimit.truncated"]:
                terminal_idx.append(idx)
                terminal_obs.append(inf["terminal_observation"])
        if len(terminal_obs) > 0:
            terminal_obs = _unflatten_obs(self.obs_normalizer(_flatten_obs(terminal_obs)))

        # collect the true next states and masks (accounting for time truncation)
        true_next_obs = _unflatten_obs(next_obs)
        true_mask = mask.copy()
        for idx, term_ob in zip(terminal_idx, terminal_obs):
            true_next_obs[idx] = term_ob
            true_mask[idx] = 1.0
        true_next_obs = _flatten_obs(true_next_obs)

        self.buffer.push({
            "obs": obs,
            "act": act,
            "rew": rew,
            # "next_obs": next_obs,
            # "mask": mask,
            "next_obs": true_next_obs,
            "mask": true_mask,
            "c": c
        })
        obs = next_obs
        c = np.array([inf["constraint_values"] for inf in info["n"]])

        self.obs = obs
        self.c = c

        self.total_steps += self.rollout_batch_size

        # learn
        results = defaultdict(list)
        if self.total_steps > self.warm_up_steps and not self.total_steps % self.train_interval:
            # Regardless of how long you wait between updates,
            # the ratio of env steps to gradient steps is locked to 1.
            # alternatively, can update once each step
            for _ in range(self.train_interval):
                batch = self.buffer.sample(self.train_batch_size, self.device)
                res = self.agent.update(batch)
                for k, v in res.items():
                    results[k].append(v)

        results = {k: sum(v) / len(v) for k, v in results.items()}
        results.update({"step": self.total_steps, "elapsed_time": time.time() - start})
        return results

    def log_step(self, results):
        """Does logging after a training step."""
        step = results["step"]
        final_step = self.constraint_epochs if self.pretraining else self.max_env_steps

        # runner stats
        self.logger.add_scalars(
            {
                "step": step,
                "time": results["elapsed_time"],
                "progress": step / final_step,
            },
            step,
            prefix="time",
            write=False,
            write_tb=False)
        if self.pretraining:
            # Constraint learning stats.
            for i in range(self.safety_layer.num_constraints):
                name = "constraint_{}_loss".format(i)
                self.logger.add_scalars({name: results[name]}, step, prefix="constraint_loss")
                if "eval" in results:
                    self.logger.add_scalars({name: results["eval"][name]}, step, prefix="constraint_loss_eval")
        else:
            # learning stats
            if "policy_loss" in results:
                self.logger.add_scalars(
                    {
                        k: results[k]
                        for k in ["policy_loss", "critic_loss"]
                    },
                    step,
                    prefix="loss")

            # performance stats
            ep_lengths = np.asarray(self.env.length_queue)
            ep_returns = np.asarray(self.env.return_queue)
            ep_constraint_violation = np.asarray(self.env.queued_stats["constraint_violation"])
            self.logger.add_scalars(
                {
                    "ep_length": ep_lengths.mean(),
                    "ep_return": ep_returns.mean(),
                    "ep_reward": (ep_returns / ep_lengths).mean(),
                    "ep_constraint_violation": ep_constraint_violation.mean()
                },
                step,
                prefix="stat")

            # total constraint violation during learning
            total_violations = self.env.accumulated_stats["constraint_violation"]
            self.logger.add_scalars({"constraint_violation": total_violations}, step, prefix="stat")

            if "eval" in results:
                eval_ep_lengths = results["eval"]["ep_lengths"]
                eval_ep_returns = results["eval"]["ep_returns"]
                eval_constraint_violation = results["eval"]["constraint_violation"]
                eval_mse = results["eval"]["mse"]
                self.logger.add_scalars(
                    {
                        "ep_length": eval_ep_lengths.mean(),
                        "ep_return": eval_ep_returns.mean(),
                        "ep_reward": (eval_ep_returns / eval_ep_lengths).mean(),
                        "constraint_violation": eval_constraint_violation.mean(),
                        "mse": eval_mse.mean()
                    },
                    step,
                    prefix="stat_eval")

        # print summary table
        self.logger.dump_scalars()



    def collect_constraint_data(self,
                                num_steps
                                ):
        """Uses random policy to collect data for pre-training constriant models.

        """
        step = 0
        obs, info = self.env.reset()
        obs = self.obs_normalizer(obs)
        c = np.array([inf["constraint_values"] for inf in info["n"]])
        while step < num_steps:
            action_spaces = self.env.get_attr("action_space")
            action = np.array([space.sample() for space in action_spaces])
            obs_next, _, done, info = self.env.step(action)
            obs_next = self.obs_normalizer(obs_next)
            c_next = []
            for i, d in enumerate(done):
                if d:
                    c_next_i = info["n"][i]["terminal_info"]["constraint_values"]
                else:
                    c_next_i = info["n"][i]["constraint_values"]
                c_next.append(c_next_i)
            c_next = np.array(c_next)
            self.constraint_buffer.push({"act": action, "obs": obs, "c": c, "c_next": c_next})
            obs = obs_next
            c = np.array([inf["constraint_values"] for inf in info["n"]])
            step += self.rollout_batch_size

    def eval_constraint_models(self):
        """Runs evaluation for the constraint models.

        """
        eval_resutls = defaultdict(list)
        self.safety_layer.eval()
        self.obs_normalizer.set_read_only()
        # Collect evaluation data.
        self.collect_constraint_data(self.constraint_eval_steps)
        for batch in self.constraint_buffer.sampler(self.constraint_batch_size):
            losses = self.safety_layer.compute_loss(batch)
            for i, loss in enumerate(losses):
                eval_resutls["constraint_{}_loss".format(i)].append(loss.item())
        self.constraint_buffer.reset()
        eval_resutls = {k: sum(v) / len(v) for k, v in eval_resutls.items()}
        return eval_resutls
