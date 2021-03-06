import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.multiprocessing as mp
import gym
import os
import matplotlib.pyplot as plt
import numpy as np
import cv2
import logging

from algorithms._interface import RLInterface
from utils import np_torch_wrap, SharedAdam, SharedRMSprop


class Model(nn.Module):
    def __init__(self, input_shape, n_actions, stack_frames=4):
        super(Model, self).__init__()

        # A shared convolution body
        self.conv = nn.Sequential(
            nn.Conv2d(stack_frames, 32, kernel_size=8, stride=4),
            nn.ReLU(),
            nn.Conv2d(32, 64, kernel_size=4, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 64, kernel_size=3, stride=1),
            nn.ReLU()
        )
        
        conv_out_size = int(np.prod(self.conv(torch.zeros(1, *input_shape)).size()))

        # First head is returning the policy with probability distribution over actions
        self.policy = nn.Sequential(
            nn.Linear(conv_out_size, 512),
            nn.ReLU(),
            nn.Linear(512, n_actions)
        )

        # Second head returns one single number (approximate state's value)
        self.value = nn.Sequential(
            nn.Linear(conv_out_size, 512),
            nn.ReLU(),
            nn.Linear(512, 1)
        )

        self.distribution = torch.distributions.Categorical

    def forward(self, x):
        # Returns a tuple of two tensors: policy and value
        fx = x.float() / 256
        conv_out = self.conv(fx).view(fx.size()[0], -1)
        return self.policy(conv_out), self.value(conv_out)

    def choose_action(self, s):
        self.eval()
        logits, _ = self.forward(s)
        prob = F.softmax(logits, dim=1).data
        m = self.distribution(prob)
        return m.sample().numpy()[0]

    def loss_func(self, states, actions, target_values):
        self.train()
        logits, values = self.forward(states)

        advantage = target_values - values
        value_loss = advantage.pow(2)
        
        probs = F.softmax(logits, dim=1)
        m = self.distribution(probs)
        exp_v = m.log_prob(actions) * advantage.detach().squeeze()  # log pi * A
        policy_loss = -exp_v
        total_loss = (value_loss + policy_loss).mean()  # mean squared error

        # detach all except total_loss as it will be used later
        return total_loss, value_loss.detach().mean(), policy_loss.detach().mean(), advantage.detach().mean()


class Worker(mp.Process):
    def __init__(
        self, 
        worker_name,
        env_factory,
        f_checkpoint,
        f_sync,
        res_queue,
        global_network,
        global_ep_counter,
        update_global_delay=20,
        max_eps=10000,
        max_length=1000,
        n_s=None,
        n_a=None,
        render=False):

        super(Worker, self).__init__()

        # local worker config
        self.name = 'w%i' % worker_name
        self.logprefix = "\033[0;1mWorker %s:\033[0m " % self.name
        self.render = render
        self.max_eps = max_eps  # max episodes of all workers
        self.max_length = max_length
        self.update_global_delay = update_global_delay
        self.global_ep_counter = global_ep_counter
        self.res_queue = res_queue  # shared queue to store results

        # callback global functions
        self.synchronize = f_sync  # push local gradients to global network
        self.checkpoint = f_checkpoint  # calculate statistics and save paramenters when improvements are achieved

        logging.info(self.logprefix + "Instantiating environment...")
        self.env = env_factory()
        env_temp = env_factory()
        env_shape = env_temp.reset().shape
        self.local_network = Model(n_s if n_s is not None else env_shape, n_a if n_a is not None else self.env.n_actions, self.env.stack_frames)  # local network
        # synchronize thread-specific parameters Θ' = Θ and Θ'v = Θv
        self.local_network.load_state_dict(global_network.state_dict())

    def run(self):
        logging.info(self.logprefix + "Running...")
        thread_step = 1  # initialize thread step counter
        while self.global_ep_counter.value < self.max_eps:  # repeat until T < Tmax
            # here we don't reset gradients or synchronize thread-specific parameters with
            # the global network - this will be treated in "self.synchronize" function 
            buffer_state, buffer_action, buffer_reward = [], [], []
            episode_reward = 0.
            episode_step = 0  # Tstart = T (this is equivalent, but easier to understand)
            total_loss = 0
            total_value_loss = 0
            total_policy_loss = 0
            total_advantage = 0
            gradient_updates = 0

            state = self.env.reset()  # get state St
            while episode_step < self.max_length:  # repeat until terminal or T-Tstart==Tmax
                if self.render and self.name == 'w0':
                    self.env.render()

                action = self.local_network.choose_action(np_torch_wrap(state[None, :]))  # perform At according to local policy
                new_state, reward, done, _ = self.env.step(action if action < self.env.n_actions else 0)  # receive reward Rt and new state St+1
                if done: reward = -1
                episode_reward += reward  # accumulate reward
                buffer_action.append(action)
                buffer_state.append(state)
                buffer_reward.append(reward)

                if thread_step % self.update_global_delay == 0 or done:  # update global and assign to local net
                    # calculate R
                    # for... accumulate gradients ... end for
                    # perform asynchronous update on global network
                    # send all last states/actions/rewards function to calculate accumulated gradients and push it to the global network
                    loss, mean_value_loss, mean_policy_loss, mean_advantage = self.synchronize(self.local_network, done, new_state, buffer_state, buffer_action, buffer_reward)

                    gradient_updates += 1
                    total_loss += loss
                    total_value_loss += mean_value_loss
                    total_policy_loss += mean_policy_loss
                    total_advantage += mean_advantage

                    buffer_state, buffer_action, buffer_reward = [], [], []

                    if done: break

                state = new_state
                thread_step += 1  # t = t + 1
                episode_step += 1  # T = T + 1

            # save statistics of reward per episode, episode length, mean loss and gradient updates
            self.checkpoint(
                self.name, 
                episode_reward, 
                episode_step, 
                total_loss / gradient_updates, 
                total_value_loss / gradient_updates,
                total_policy_loss / gradient_updates,
                total_advantage / gradient_updates,
                gradient_updates
            )

        self.res_queue.put(None)


class A3C(RLInterface):
    def __init__(
        self, 
        env_factory,
        play=False,
        save_load_path = "trained_models", 
        skip_load = False,
        render = False,
        n_workers = mp.cpu_count(), 
        gamma = 0.9, 
        update_global_delay = 20,
        checkpoint_interval=10,
        max_eps = 10000,
        max_length = 1000,
        random = False):

        super(A3C, self).__init__()

        self.name = "A3C_Conv"
        self.logprefix = "\033[0;1mA3C Global: \033[0m"
        self.env_factory = env_factory
        self.random = random

        # init temp env to get it's properties
        logging.info(self.logprefix + "Instantiating environment...")
        env = env_factory[0]() if type(env_factory) is list else env_factory()
        env_shape = env.reset().shape
        self.env_name = env.name  # to save/load
        
        # free attributes
        self.checkpoint_interval = checkpoint_interval
        self.render = render
        self.gamma = gamma
        self.save_load_path = save_load_path
        
        # initialize global network
        self.global_network = Model(env_shape, env.n_actions, env.stack_frames)
        self.global_network.share_memory()  # share the global parameters in multiprocessing
        self.optimizer = SharedRMSprop(self.global_network.parameters(), lr=0.0001)  # global optimizer

        self.init_writer()  # instantiate tensorboard writer

        # load network, optimizer, episode count and max_reward
        if not skip_load:
            self.load(not play)  # if we want to play we play with the best player, not the last one

        self.global_ep_counter = mp.Value('i', self.episode)  # this is needed to control workers episode limit
        self.global_ep_reward = mp.Value('d', 0.)  # current episode reward
        self.res_queue = mp.Queue()  # queue to receive workers statistics

        # instantiate workers
        self.workers = [
            Worker(
                worker_name=i,
                # assign a random environment for each worker, if multiple envs are received 
                env_factory = env_factory[np.random.randint(0, len(env_factory) - 1)] if type(env_factory) is list else env_factory, 
                f_checkpoint = self.checkpoint,
                f_sync = self.sync,
                res_queue = self.res_queue,
                global_network = self.global_network,
                global_ep_counter = self.global_ep_counter, 
                update_global_delay = update_global_delay, 
                max_eps = max_eps,
                max_length = 1000, 
                n_s = None,
                n_a = env.n_actions,
                render = render
            ) for i in range(n_workers)
        ]

    def run(self):
        """
        This method only runs on the main process.
        """

        super(A3C, self).run()

        logging.info(self.logprefix + "Running workers")

        [w.start() for w in self.workers]

        while True:
            try:  # avoid res_queue file deletion error
                r = self.res_queue.get()
                if r is not None:
                    self.episode += 1
                    self.record(
                        message=r["worker_name"],
                        episode=self.episode, 
                        reward=r["reward"],
                        episode_length=r["episode_length"],
                        mean_loss=r["mean_loss"],
                        gradient_updates=r["gradient_updates"],
                        mean_value_loss=r["mean_value_loss"],
                        mean_policy_loss=r["mean_policy_loss"],
                        mean_advantage=r["mean_advantage"]
                    )
                else:
                    break
            except Exception as e:
                logging.error(str(e))
                break

        [w.join() for w in self.workers]
        

    def sync(self, local_network, done, new_state, buffer_state, buffer_action, buffer_reward):
        """
        Remember: This method is called locally on all worker processes
        It works because:
        - Optimizer is shared
        - Global network is shared
        - We don't update self.gamma
        TODO: move this code to the Worker's run loop to better match the paper and copy less data
        """

        # calculate R
        if done:
            R = 0.  # for terminal St
        else: # for non-terminal St // Bootstrap from last state
            R = local_network.forward(np_torch_wrap(new_state[None, :]))[-1].data.numpy()[0, 0]

        # for i E {t - 1, ..., Tstart}
        buffer_v_target = []
        for r in buffer_reward[::-1]:    # reverse buffer r
            R = r + self.gamma * R
            buffer_v_target.append(R)
        buffer_v_target.reverse()
        
        # accumulate gradients
        loss, mean_value_loss, mean_policy_loss, mean_advantage = local_network.loss_func(
            np_torch_wrap(np.array(buffer_state)),
            np_torch_wrap(np.array(buffer_action), dtype=np.int64) if buffer_action[0].dtype == np.int64 else np_torch_wrap(np.vstack(buffer_action)),
            np_torch_wrap(np.array(buffer_v_target)[:, None])
        )
        
        # perform asynchronous update of Θ using dΘ and of Θv using dΘv
        self.optimizer.zero_grad()
        loss.backward()
        for lp, gp in zip(local_network.parameters(), self.global_network.parameters()):
            gp._grad = lp.grad
        self.optimizer.step()

        # synchronize thread-specific parameters Θ' = Θ and Θ'v = Θv
        local_network.load_state_dict(self.global_network.state_dict())

        return loss.detach(), mean_value_loss, mean_policy_loss, mean_advantage

    def checkpoint(
        self, 
        worker_name, 
        episode_reward, 
        episode_length, 
        mean_loss,
        mean_value_loss,
        mean_policy_loss,
        mean_advantage,
        gradient_updates):
        """
        Remember: This method is called locally on all worker processes
        It works because we only use shared variables and get their respective locks to update them.
        """

        # increment global episode counter
        with self.global_ep_counter.get_lock():
            self.global_ep_counter.value += 1

        # update global moving average reward
        with self.global_ep_reward.get_lock():
            self.global_ep_reward.value = episode_reward

        self.res_queue.put({
            "worker_name": worker_name,
            "reward": self.global_ep_reward.value,
            "episode_length": episode_length,
            "mean_loss": mean_loss,
            "mean_value_loss": mean_value_loss,
            "mean_policy_loss": mean_policy_loss,
            "mean_advantage": mean_advantage,
            "gradient_updates": gradient_updates
        })
