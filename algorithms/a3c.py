import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.multiprocessing as mp
import gym
import os
import matplotlib.pyplot as plt
import numpy as np
import cv2
import random
from algorithms._interface import RLInterface
from utils import np_torch_wrap, SharedAdam, SharedRMSprop
import logging


class Model(nn.Module):
    def __init__(self, s_dim, a_dim):
        super(Model, self).__init__()
        self.s_dim = s_dim
        self.a_dim = a_dim
        self.pi1 = nn.Linear(s_dim, 800) # (X, S_DIM) * (S_DIM, 200)
        self.pi2 = nn.Linear(800, a_dim)
        self.v1 = nn.Linear(s_dim, 600)
        self.v2 = nn.Linear(600, 1)
        [(nn.init.normal_(layer.weight, mean=0., std=0.1), nn.init.constant_(layer.bias, 0.)) for layer in [self.pi1, self.pi2, self.v1, self.v2]]
        self.distribution = torch.distributions.Categorical

    def forward(self, x):
        pi1 = F.relu6(self.pi1(x))
        logits = self.pi2(pi1)
        v1 = F.relu6(self.v1(x))
        values = self.v2(v1)
        return logits, values

    def choose_action(self, s):
        self.eval()
        logits, _ = self.forward(s)
        prob = F.softmax(logits, dim=1).data
        m = self.distribution(prob)
        return m.sample().numpy()[0]

    def loss_func(self, s, a, v_t):
        self.train()
        logits, values = self.forward(s)
        td = v_t - values
        c_loss = td.pow(2)
        
        probs = F.softmax(logits, dim=1)
        m = self.distribution(probs)
        exp_v = m.log_prob(a) * td.detach().squeeze()
        a_loss = -exp_v
        total_loss = (c_loss + a_loss).mean()
        return total_loss


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
        self.local_network = Model(n_s if n_s is not None else self.env.n_obs, n_a if n_a is not None else self.env.n_actions)  # local network
        # synchronize thread-specific parameters Θ' = Θ and Θ'v = Θv
        self.local_network.load_state_dict(global_network.state_dict())

    def run(self):
        thread_step = 1  # initialize thread step counter
        while self.global_ep_counter.value < self.max_eps:  # repeat until T < Tmax
            # here we don't reset gradients or synchronize thread-specific parameters with
            # the global network - this will be treated in "self.synchronize" function 
            buffer_s, buffer_a, buffer_r = [], [], []
            episode_reward = 0.
            episode_step = 0  # Tstart = T (this is equivalent, but easier to understand)
            state = self.env.reset()  # get state St
            while episode_step < self.max_length:  # repeat until terminal or T-Tstart==Tmax
                if self.render and self.name == 'w0':
                    self.env.render()

                action = self.local_network.choose_action(np_torch_wrap(state[None, :]))  # perform At according to local policy
                new_state, r, done, _ = self.env.step(action if action < self.env.n_actions else 0)  # receive reward Rt and new state St+1
                if done: r = -1
                episode_reward += r  # accumulate reward
                buffer_a.append(action)
                buffer_s.append(state)
                buffer_r.append(r)

                if thread_step % self.update_global_delay == 0 or done:  # update global and assign to local net
                    # calculate R
                    # for... accumulate gradients ... end for
                    # perform asynchronous update on global network
                    # send all last states/actions/rewards function to calculate accumulated gradients and push it to the global network
                    self.synchronize(self.local_network, done, new_state, buffer_s, buffer_a, buffer_r)
                    buffer_s, buffer_a, buffer_r = [], [], []

                    if done: break  # done and print information

                state = new_state
                thread_step += 1  # t = t + 1
                episode_step += 1  # T = T + 1

            self.checkpoint(episode_reward, self.name, episode_step)

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
        max_length = 1000):

        super(A3C, self).__init__()

        self.name = "A3C_Conv"
        self.logprefix = "\033[0;1mA3C Global: \033[0m"
        self.env_factory = env_factory

        # init temp env to get it's properties
        logging.info(self.logprefix + "Instantiating environment...")
        env = env_factory[0]() if type(env_factory) is list else env_factory()
        self.env_name = env.name  # to save/load
        
        # free attributes
        self.checkpoint_interval = checkpoint_interval
        self.render = render
        self.gamma = gamma
        self.save_load_path = save_load_path
        
        # initialize global network
        self.global_network = Model(env.n_obs, env.n_actions)
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
                env_factory = env_factory[random.randint(0, len(env_factory) - 1)] if type(env_factory) is list else env_factory, 
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

        self.init_writer()  # instantiate tensorboard writer

    def run(self):
        """
        This method only runs on the main process.
        """

        super(A3C, self).run()

        logging.info(self.logprefix + "Running workers")

        [w.start() for w in self.workers]

        while True:
            r = self.res_queue.get()
            if r is not None:
                self.episode += 1
                self.record(
                    message=r["worker_name"],
                    episode=self.episode, 
                    reward=r["reward"],
                    episode_length=r["episode_length"],
                )
            else:
                break

        [w.join() for w in self.workers]

    def sync(self, local_network, done, s_, bs, ba, br):
        # calculate R
        if done:
            R = 0.  # for terminal St
        else: # for non-terminal St // Bootstrap from last state
            R = local_network.forward(np_torch_wrap(s_[None, :]))[-1].data.numpy()[0, 0]

        # for i E {t - 1, ..., Tstart}
        buffer_v_target = []
        for r in br[::-1]:    # reverse buffer r
            R = r + self.gamma * R
            buffer_v_target.append(R)
        buffer_v_target.reverse()

        # accumulate gradients
        loss = local_network.loss_func(
            np_torch_wrap(np.vstack(bs)),
            np_torch_wrap(np.array(ba), dtype=np.int64) if ba[0].dtype == np.int64 else np_torch_wrap(np.vstack(ba)),
            np_torch_wrap(np.array(buffer_v_target)[:, None]))

        # perform asynchronous update of Θ using dΘ and of Θv using dΘv
        self.optimizer.zero_grad()
        loss.backward()
        for lp, gp in zip(local_network.parameters(), self.global_network.parameters()):
            gp._grad = lp.grad
        self.optimizer.step()

        # synchronize thread-specific parameters Θ' = Θ and Θ'v = Θv
        local_network.load_state_dict(self.global_network.state_dict())

    def checkpoint(self, episode_reward, worker_name, episode_length):
        # increment global episode counter
        with self.global_ep_counter.get_lock():
            self.global_ep_counter.value += 1

        # update global moving average reward
        with self.global_ep_reward.get_lock():
            self.global_ep_reward.value = episode_reward

        self.res_queue.put({
            "worker_name": worker_name,
            "reward": self.global_ep_reward.value,
            "episode": self.global_ep_counter.value,
            "episode_length": episode_length
        })