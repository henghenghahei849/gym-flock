import gym
from gym import spaces
from gym.utils import seeding
import numpy as np
import copy
import matplotlib.pyplot as plt
from matplotlib.pyplot import gca
from gym.spaces import Box

from gym_flock.envs.spatial.make_map import generate_lattice, generate_geometric_roads
from gym_flock.envs.spatial.utils import _get_graph_edges, _get_k_edges, _get_pos_diff, _nodes_within_radius
from gym_flock.envs.spatial.vrp_solver import solve_vrp

from scipy.sparse.csgraph import connected_components
from scipy.sparse import csr_matrix

try:
    import tensorflow as tf
except ImportError:
    tf = None

try:
    import ortools
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
except ImportError:
    ortools = None

font = {'family': 'sans-serif',
        'weight': 'bold',
        'size': 14}

# number of node and edge features
N_NODE_FEAT = 3
N_EDGE_FEAT = 1
N_GLOB_FEAT = 1

HIDE_NODES = False
REVISIT_NODES = False
COLLISION_CHECKS = True
COMM_EDGES = False
USE_NODE_HISTORY = False

LAST_EDGE_FEATURE = False
USE_POS_DELTA = False
# USE_ROBOT_IDS = False

# NEARBY_STARTS = False
NEARBY_STARTS = True
NEARBY_DENSITY = 5
# N_HOP_EDGES = 1

# padding for a variable number of graph edges
PAD_NODES = True
MAX_NODES = 500
MAX_EDGES = 4

# number of edges/actions for each robot, fixed
PAD_ACTIONS = True
N_ACTIONS = 4
# ALLOW_NEAREST = False
GREEDY_CONTROLLER = False

EPISODE_LENGTH = 75

HORIZON = 10

MAX_COST = 1000

N_ROBOTS = 6

XMAX = 120
YMAX = 120

FRAC_ACTIVE = 0.5
MIN_FRAC_ACTIVE = 0.5

unvisited_regions = [(-100, 100, -100, 100)]
start_regions = [(-100, 100, -100, 100)]
DELTA = 5.5

class CoverageEnv(gym.Env):
    def __init__(self, n_robots=N_ROBOTS, frac_active_targets=FRAC_ACTIVE, xmax=XMAX, ymax=YMAX,
                 starts=start_regions, unvisiteds=unvisited_regions, init_graph=True, episode_length=EPISODE_LENGTH,
                 res=DELTA, pad_nodes=PAD_NODES, max_nodes=MAX_NODES, nearby_starts=NEARBY_STARTS, horizon=HORIZON, hide_nodes=HIDE_NODES, n_node_feat=N_NODE_FEAT):
        """Initialize the mapping environment
        """
        super(CoverageEnv, self).__init__()

        self.keys = ['nodes', 'edges', 'senders', 'receivers', 'step']
        self.n_node_feat = n_node_feat

        self.hide_nodes = hide_nodes
        self.horizon = horizon
        self.episode_length = episode_length
        self.nearby_starts = nearby_starts

        self.pad_nodes = pad_nodes
        self.max_nodes = max_nodes

        self.y_min = -ymax
        self.x_min = -xmax
        self.x_max = xmax
        self.y_max = ymax

        self.res = res

        self.start_ranges = starts
        self.unvisited_ranges = unvisiteds

        # # triangular lattice
        # self.lattice_vectors = [
        #     2.75 * np.array([-1.414, -1.414]),
        #     2.75 * np.array([-1.414, 1.414])]

        # square lattice
        self.lattice_vectors = [
            np.array([-DELTA, 0.]),
            np.array([0., -DELTA])]

        self.np_random = None
        self.seed()

        # dim of state per agent, 2D position and 2D velocity
        self.nx = 2

        # agent dynamics are controlled with 2D acceleration
        self.nu = 2

        # number of robots and targets
        self.n_robots = n_robots
        self.frac_active_targets = frac_active_targets

        # graph parameters
        self.comm_radius = 100.0
        self.motion_radius = self.res * 1.2
        self.obs_radius = self.res * 1.2

        # call helper function to initialize arrays
        if init_graph:
            targets, _ = self._generate_targets()
            self._initialize_graph(targets)

        # plotting and seeding parameters
        self.episode_reward = 0

        self.fig = None
        self._plot_text = None
        self.ax = None
        self.line1 = None
        self.line2 = None
        self.line3 = None
        self.line4 = None
        self.line5 = None

        self.step_counter = 0
        self.n_motion_edges = 0

        self.last_loc = None
        self.node_history = None

        self.cached_solution = None
        self.graph_previous = None
        self.graph_cost = None

    def seed(self, seed=None):
        """ Seed the numpy random number generator
        :param seed: random seed
        :return: random seed
        """
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def step(self, action):
        """ Simulate a single step of the environment dynamics
        The output is observations, cost, done_flag, options
        :param u_ind: control input for robots
        :return: described above
        """
        if action is not None:
            if type(action) == np.ndarray:
                action = action.flatten().tolist()

            self.last_loc = self.closest_targets

            next_locs = [-1] * len(action)  #copy.copy(action)
            for i in range(self.n_robots):
                cur_robot_edges = np.where(self.mov_edges[0] == i)
                next_loc = self.mov_edges[1][cur_robot_edges][action[i]]
                if next_loc == self.last_loc[i]:
                    next_locs[i] = next_loc

            for i in range(self.n_robots):
                if next_locs[i] == -1:
                    next_loc = self.mov_edges[1][np.where(self.mov_edges[0] == i)][action[i]]
                    if not COLLISION_CHECKS or next_loc not in next_locs:
                        next_locs[i] = next_loc
                        self.x[i, 0:2] = self.x[next_loc, 0:2]
                    else:
                        next_locs[i] = self.last_loc[i]

        obs, reward, done = self._get_obs_reward()
        # self.render()
        return obs, reward, done, {}

    def get_action_edges(self):
        """
        Compute edges from robots to nearby landmarks, and pad with loops to the current locations up to N_ACTIONS
        :return: adjacency list, edge distances
        """
        senders = np.zeros((0,))
        receivers = np.zeros((0,))
        curr_nodes = self.closest_targets

        for i in range(self.n_robots):
            next_nodes = self.motion_edges[1][np.where(self.motion_edges[0] == curr_nodes[i])]
            n_next_nodes = np.shape(next_nodes)[0]

            # pad edges for each robot with loops to current node
            if n_next_nodes < N_ACTIONS:
                next_nodes = np.append(next_nodes, [curr_nodes[i]] * (N_ACTIONS - n_next_nodes))

            senders = np.append(senders, [i] * 4)
            receivers = np.append(receivers, next_nodes)

        senders = senders.astype(np.int)
        receivers = receivers.astype(np.int)

        # compute edge distances
        diff = self.x[senders, :] - self.x[receivers, :]
        dists = np.linalg.norm(self.x[senders, :] - self.x[receivers, :], axis=1)
        return (senders, receivers), dists, diff

    def _get_obs_reward(self):
        """
        Method to retrieve observation graph, with node and edge attributes
        :return:
        nodes - node attributes
        edges - edge attributes
        senders - sender nodes for each edge
        receivers - receiver nodes for each edge
        reward - MDP reward at this step
        done - is this the last step of the episode?
        """

        if REVISIT_NODES:
            self.visited = np.where(self.landmark_flag * np.random.binomial(1, 0.005, size=np.shape(self.visited)) > 0.0, 0.0, self.visited)

        if PAD_ACTIONS:
            action_edges, action_dist, action_diff = self.get_action_edges()
        else:
            action_edges, action_dist, action_diff = _get_k_edges(self.n_actions, self.x[:self.n_robots, 0:2],
                                                                  self.x[self.n_robots:self.n_agents, 0:2],
                                                                  allow_nearest=False)
            action_edges = (action_edges[0], action_edges[1] + self.n_robots)

        assert len(action_edges[0]) == N_ACTIONS * self.n_robots, "Number of action edges is not num robots x n_actions"

        action_edges = (np.concatenate([action_edges[0], action_edges[1]], axis=0),
                        np.concatenate([action_edges[1], action_edges[0]], axis=0))
        action_dist = np.concatenate([action_dist, action_dist], axis=0)

        self.mov_edges = action_edges

        old_sum = np.sum(self.visited[self.n_robots:self.n_agents])
        self.visited[self.closest_targets] = 1

        if USE_NODE_HISTORY == 4:
            self.node_history[self.closest_targets] = 1

        if COMM_EDGES:
            # communication edges among robots
            comm_edges, comm_dist, comm_diff = _get_graph_edges(self.comm_radius, self.x[:self.n_robots, 0:2])
            senders = np.concatenate((action_edges[1], comm_edges[0]))
            receivers = np.concatenate((action_edges[0], comm_edges[1]))

            if not USE_POS_DELTA:
                edges_dist = np.concatenate((action_dist, comm_dist)).reshape((-1, 1))
            else:
                edges_diff = np.concatenate((np.concatenate((action_diff, comm_diff)).reshape((-1, 2)), np.concatenate((action_dist, comm_dist)).reshape((-1, 1))), axis=1)
        else:
            senders = action_edges[1]
            receivers = action_edges[0]
            if not USE_POS_DELTA:
                edges_dist = action_dist.reshape((-1, 1))
            else:
                edges_diff = np.concatenate((action_diff, action_dist.reshape((-1, 1))), axis=1)
        assert len(senders) + self.n_motion_edges <= np.shape(self.senders)[0], "Increase MAX_EDGES"

        # normalize the edge distance by resolution
        if not USE_POS_DELTA:
            edges_dist = edges_dist / self.res
        else:
            edges_diff = edges_diff / self.res

        if LAST_EDGE_FEATURE:
            last_edges = np.zeros((len(senders), 1), dtype=np.bool)
            if self.last_loc is not None:
                for i in range(self.n_robots):
                    last_edges = np.logical_or(last_edges,
                                               np.logical_and(receivers == i, senders == self.last_loc[i]).reshape(
                                                   (-1, 1)))
                    last_edges = last_edges.reshape((-1, 1))

            if not USE_POS_DELTA:
                edges = np.concatenate((last_edges, edges_dist), axis=1).reshape((-1, 2))
            else:
                edges = np.concatenate((last_edges, edges_diff), axis=1).reshape((-1, 4))

        else:
            if not USE_POS_DELTA:
                edges = edges_dist.reshape((-1, 1))
            else:
                edges = edges_diff.reshape((-1, 3))

        # -1 indicates unused edges
        self.senders[self.n_motion_edges:] = -1
        self.receivers[self.n_motion_edges:] = -1
        self.nodes.fill(0)

        self.senders[-len(senders):] = senders
        self.receivers[-len(receivers):] = receivers
        self.edges[-len(senders):, :] = edges

        self.nodes[0:self.n_agents, 0] = self.robot_flag.flatten()
        self.nodes[0:self.n_agents, 1] = self.landmark_flag.flatten()
        self.nodes[0:self.n_agents, 2] = np.logical_not(self.visited).flatten()
        if USE_NODE_HISTORY:
            self.nodes[0:self.n_agents, 3] = self.node_history.flatten()

        # if USE_ROBOT_IDS:
        #     self.nodes[0:self.n_robots, 3] = np.arange(start=1, stop=self.n_robots+1, step=1).flatten()

        if self.hide_nodes:
            seen_nodes = _nodes_within_radius(4.0 * DELTA, self.x[:self.n_robots, 0:2], self.x[0:self.n_agents, 0:2])
            self.discovered_nodes[0:self.n_agents] = (self.discovered_nodes[0:self.n_agents].reshape((-1, 1)) + seen_nodes.astype(np.float)) > 0.0
            self.nodes = self.nodes * self.discovered_nodes.reshape((-1, 1))

            frontier_node_ind = self.receivers[(1.0 - self.discovered_nodes[self.senders].flatten()) * self.discovered_nodes[self.receivers].flatten() > 0.0]
            self.nodes[frontier_node_ind, 3] = 1.0

            seen_edges = self.discovered_nodes[self.senders].flatten() * self.discovered_nodes[self.receivers].flatten()
            seen_edges[-len(senders):] = 1.0
            out_senders = np.where(seen_edges > 0, self.senders, -1)
        else:
            out_senders = self.senders

        # if N_NODE_FEAT == 4:
        #     self.nodes[0:self.n_agents, 3] = self.node_history.flatten()

        step_array = np.array([self.step_counter]).reshape((1, 1))

        obs = {'nodes': self.nodes, 'edges': self.edges, 'senders': out_senders, 'receivers': self.receivers,
               'step': step_array}

        self.step_counter += 1
        done = self.step_counter == self.episode_length or np.sum(self.visited[self.n_robots:]) == self.n_targets

        reward = np.sum(self.visited[self.n_robots:]) - old_sum
        # _, counts = np.unique(self.closest_targets, return_counts=True)
        # collision_penalty = np.sum(counts - 1)
        # reward -= collision_penalty
        self.episode_reward += reward
        return obs, reward, done

    def reset(self):
        """
        Reset system state. Agents are initialized in a square with side self.r_max
        :return: observations, adjacency matrix
        """

        self.episode_reward = 0
        self.step_counter = 0
        self.cached_solution = None
        self.last_loc = None
        self.node_history = None

        targets, graph_changed = self._generate_targets()

        if graph_changed:
            if self.fig is not None:
                plt.close(self.fig)

            # plotting and seeding parameters
            self.n_motion_edges = 0
            self.graph_previous = None
            self.graph_cost = None

            self.fig = None
            self._plot_text = None
            self.ax = None
            self.line1 = None
            self.line2 = None
            self.line3 = None
            self.line4 = None

            self._initialize_graph(targets)
        else:
            if self.nearby_starts:
                n_nearest = self.get_n_nearest(self.np_random.choice(self.n_targets), self.n_robots * NEARBY_DENSITY)
                # n_nearest = self.get_n_nearest(self.np_random.choice(self.n_targets), 200)
                self.start_region = [i in n_nearest for i in range(self.n_targets)]

        # initialize robots near targets
        nearest_landmarks = self.np_random.choice(np.arange(self.n_targets)[self.start_region], size=(self.n_robots,),
                                                  replace=False)
        # nearest_landmarks = self.np_random.choice(2 * self.n_robots, size=(self.n_robots,), replace=False)
        self.x[:self.n_robots, 0:2] = self.x[nearest_landmarks + self.n_robots, 0:2]

        unvisited_targets = np.arange(self.n_targets)[self.unvisited_region] + self.n_robots

        random_unvisited_targets = self.np_random.choice(unvisited_targets,
                                                         size=(int(len(unvisited_targets) * self.frac_active_targets),),
                                                         replace=False)

        self.visited.fill(1)
        self.visited[random_unvisited_targets] = 0

        if self.hide_nodes:
            self.discovered_nodes = np.vstack(
                (np.ones((self.n_robots, 1)), np.zeros((self.max_nodes - self.n_robots, 1))))

        self.node_history = np.zeros((self.n_agents, 1))
        obs, _, _ = self._get_obs_reward()
        return obs

    @property
    def closest_targets(self):
        r = np.linalg.norm(self.x[:self.n_robots, 0:2].reshape((self.n_robots, 1, 2))
                           - self.x[self.n_robots:, 0:2].reshape((1, self.n_targets, 2)), axis=2)
        closest_targets = np.argmin(r, axis=1) + self.n_robots
        return closest_targets

    def render(self, mode='human'):
        """
        Render the environment with agents as points in 2D space. The robots are in green, the targets in red.
        When a target has been visited, it becomes a blue dot. The plot objects are created on the first render() call
        and persist between calls of this function.
        :param mode: 'human' mode renders the environment, and nothing happens otherwise
        """
        if mode is not 'human':
            return

        if self.fig is None:
            # initialize plot parameters
            plt.ion()
            self.fig = plt.figure()
            self.ax = self.fig.add_subplot(111)

            # self._plot_text = plt.text(x=-170, y=45.0, s="", fontsize=32)
            self._plot_text = plt.text(x=100, y=100.0, s="", fontsize=32)

            for (i, j) in zip(self.motion_edges[0], self.motion_edges[1]):
                self.ax.plot([self.x[i, 0], self.x[j, 0]], [self.x[i, 1], self.x[j, 1]], 'b')

            # plot robots and targets and visited targets as scatter plot

            self.line2, = self.ax.plot([], [], 'ro', markersize=10)
            self.line3, = self.ax.plot([], [], 'bo', markersize=5)
            # self.line4, = self.ax.plot([], [], 'yo')

            self.line4, = self.ax.plot([], [], 'y.')
            self.line1, = self.ax.plot([], [], 'go', markersize=15, linewidth=0)

            self.line5, = self.ax.plot([], [], 'w.')

            a = gca()
            a.set_xticklabels(a.get_xticks(), font)
            a.set_yticklabels(a.get_yticks(), font)

        self._plot_text.set_text(str(int(self.episode_reward)))

        # update robot plot
        self.line1.set_xdata(self.x[0:self.n_robots, 0])
        self.line1.set_ydata(self.x[0:self.n_robots, 1])

        # update unvisited target plot
        if self.hide_nodes:
            unvisited = np.where((np.logical_and(self.visited[self.n_robots:] == 0, self.discovered_nodes[self.n_robots:self.n_agents] > 0)).flatten())
            visited = np.where((np.logical_and(self.visited[self.n_robots:] != 0, self.discovered_nodes[self.n_robots:self.n_agents] > 0)).flatten())
        else:
            unvisited = np.where((self.visited[self.n_robots:] == 0).flatten())
            visited = np.where((self.visited[self.n_robots:] != 0).flatten())

        if self.hide_nodes:
            self.line5.set_xdata(self.x[:self.n_agents, 0][self.nodes[:self.n_agents, 3] > 0])
            self.line5.set_ydata(self.x[:self.n_agents, 1][self.nodes[:self.n_agents, 3] > 0])

        self.line2.set_xdata(self.x[self.n_robots:, 0][unvisited])
        self.line2.set_ydata(self.x[self.n_robots:, 1][unvisited])

        # update visited target plot
        self.line3.set_xdata(self.x[self.n_robots:, 0][visited])
        self.line3.set_ydata(self.x[self.n_robots:, 1][visited])

        horizon = self.horizon
        if self.graph_cost is not None and horizon > -1:
            robot_ind = self.closest_targets[0] - self.n_robots
            neighborhood = np.where((self.graph_cost[robot_ind, :] <= horizon).flatten())
            self.line4.set_xdata(self.x[self.n_robots:, 0][neighborhood])
            self.line4.set_ydata(self.x[self.n_robots:, 1][neighborhood])

        # self.line4.set_xdata([self.x[0, 0]])  # TODO
        # self.line4.set_ydata([self.x[0, 1]])

        # draw updated figure
        self.fig.canvas.draw()
        self.fig.canvas.flush_events()

    def close(self):
        """
        Close the environment
        """
        pass

    def _generate_targets(self):
        lattice = generate_lattice((self.x_min, self.x_max, self.y_min, self.y_max), self.lattice_vectors)
        n_cities = 12
        # intercity_radius = self.x_max/6
        roads = generate_geometric_roads(n_cities, self.x_max, self.motion_radius)
        flag = np.min(np.linalg.norm(_get_pos_diff(lattice, roads), axis=2), axis=1) <= (self.motion_radius / 1.4)
        targets = lattice[flag, :]
        r = np.linalg.norm(_get_pos_diff(targets), axis=2)
        r[r > self.motion_radius] = 0
        _, labels = connected_components(csgraph=csr_matrix(r), directed=False, return_labels=True)
        targets = targets[labels == np.argmax(np.bincount(labels)), :]
        return targets, True

    def _initialize_graph(self, targets):
        """
        Initialization code that is needed after params are re-loaded
        """

        self.n_targets = np.shape(targets)[0]
        self.n_agents = self.n_targets + self.n_robots
        self.x = np.zeros((self.n_agents, self.nx))
        self.x[self.n_robots:, 0:2] = targets

        # self.max_edges = self.n_agents * MAX_EDGES
        if not self.pad_nodes:
            self.max_nodes = self.n_agents

        self.max_edges = self.max_nodes * MAX_EDGES
        self.agent_type = np.vstack((np.ones((self.n_robots, 1)), np.zeros((self.n_targets, 1))))

        self.robot_flag = np.vstack((np.ones((self.n_robots, 1)), np.zeros((self.n_targets, 1))))

        self.landmark_flag = np.vstack((np.zeros((self.n_robots, 1)), np.ones((self.n_targets, 1))))
        self.n_actions = N_ACTIONS

        self.edges = np.zeros((self.max_edges, N_EDGE_FEAT), dtype=np.float32)
        self.nodes = np.zeros((self.max_nodes, self.n_node_feat), dtype=np.float32)
        self.senders = -1 * np.ones((self.max_edges,), dtype=np.int32)
        self.receivers = -1 * np.ones((self.max_edges,), dtype=np.int32)

        if self.hide_nodes:
            self.discovered_nodes = np.vstack(
                (np.ones((self.n_robots, 1)), np.zeros((self.max_nodes - self.n_robots, 1))))
        # else:
        #     self.discovered_nodes = np.ones((self.max_nodes, 1))

        # communication radius squared
        self.comm_radius2 = self.comm_radius * self.comm_radius

        # initialize state matrices
        self.visited = np.ones((self.n_agents, 1))

        self.unvisited_region = [True] * (self.n_agents - self.n_robots)

        self.agent_ids = np.reshape((range(self.n_agents)), (-1, 1))

        self.motion_edges, self.motion_dist, self.motion_diff = _get_graph_edges(self.motion_radius,
                                                                                 self.x[self.n_robots:, 0:2],
                                                                                 self_loops=True)
        # cache motion edges
        self.motion_edges = (self.motion_edges[0] + self.n_robots, self.motion_edges[1] + self.n_robots)
        self.n_motion_edges = len(self.motion_edges[0])

        # if N_HOP_EDGES > 1:
        #     self.graph_cost, self.graph_previous = self.construct_time_matrix()
        #     sender, receiver = np.where(self.graph_cost <= N_HOP_EDGES)
        #     costs = self.graph_cost[(sender, receiver)]
        #     self.n_motion_edges = len(sender)
        #
        #     self.senders[:self.n_motion_edges] = sender + self.n_robots
        #     self.receivers[:self.n_motion_edges] = receiver + self.n_robots
        #     self.edges[:self.n_motion_edges, 0] = costs
        # else:
        self.senders[:self.n_motion_edges] = self.motion_edges[0]
        self.receivers[:self.n_motion_edges] = self.motion_edges[1]
        if not USE_POS_DELTA:
            self.edges[:self.n_motion_edges, 0] = self.motion_dist.reshape((-1,))
        else:
            self.edges[:self.n_motion_edges, 0:2] = self.motion_diff.reshape((-1,2))

        if self.nearby_starts:
            n_nearest = self.get_n_nearest(self.np_random.choice(self.n_targets), self.n_robots * NEARBY_DENSITY)
            # n_nearest = self.get_n_nearest(self.np_random.choice(self.n_targets), 200)
            self.start_region = [i in n_nearest for i in range(self.n_targets)]
        else:
            self.start_region = [True] * (self.n_agents - self.n_robots)

        # problem's observation and action spaces
        self.action_space = spaces.MultiDiscrete([self.n_actions] * self.n_robots)

        if self.pad_nodes:
            nodes_space = Box(shape=(self.max_nodes, self.n_node_feat), low=-np.Inf, high=np.Inf, dtype=np.float32)
        else:
            nodes_space = Box(shape=(self.n_agents, self.n_node_feat), low=-np.Inf, high=np.Inf, dtype=np.float32)

        self.observation_space = gym.spaces.Dict(
            [
                ("nodes", nodes_space),
                ("edges", Box(shape=(self.max_edges, N_EDGE_FEAT), low=-np.Inf, high=np.Inf, dtype=np.float32)),
                ("senders", Box(shape=(self.max_edges, 1), low=0, high=self.n_agents, dtype=np.float32)),
                ("receivers", Box(shape=(self.max_edges, 1), low=0, high=self.n_agents, dtype=np.float32)),
                ("step", Box(shape=(1, 1), low=0, high=EPISODE_LENGTH, dtype=np.float32)),
            ]
        )

    def construct_time_matrix(self, edge_time=1.0):
        """
        Compute the distance between all pairs of nodes in the graph
        :param edges: list of edges provided as (sender, receiver)
        :param edge_time: uniform edge cost, assumed to be 1.0
        :return:
        """
        edges = (self.motion_edges[0] - self.n_robots, self.motion_edges[1] - self.n_robots)
        time_matrix = np.ones((self.n_targets, self.n_targets)) * np.Inf
        prev = np.ones((self.n_targets, self.n_targets), dtype=int) * -1
        np.fill_diagonal(time_matrix, 0.0)
        changed_last_iter = True  # prevents looping forever in disconnected graphs

        n_steps = 0

        while changed_last_iter and np.sum(time_matrix) == np.Inf:
            changed_last_iter = False
            for (sender, receiver) in zip(edges[0], edges[1]):
                new_cost = np.minimum(time_matrix[:, sender] + edge_time, time_matrix[:, receiver])

                prev[:, receiver] = np.where(time_matrix[:, sender] + edge_time < time_matrix[:, receiver],
                                             sender, prev[:, receiver])

                changed_last_iter = changed_last_iter or (not np.array_equal(new_cost, time_matrix[:, receiver]))
                time_matrix[:, receiver] = new_cost

            n_steps += 1
            if n_steps > self.horizon > -1:
                break

        time_matrix = np.nan_to_num(time_matrix, posinf=MAX_COST)

        return time_matrix, prev

    def get_n_nearest(self, i, n):
        """
        Get n nearest nodes
        :param i:
        :param n:
        :return:
        """
        n_nearest = set([i])

        edges = (self.motion_edges[0] - self.n_robots, self.motion_edges[1] - self.n_robots)

        while len(n_nearest) < n:
            temp = set()
            for (sender, receiver) in zip(edges[0], edges[1]):
                if sender in n_nearest:
                    temp.add(receiver)
            n_nearest = n_nearest.union(temp)

        return n_nearest

    @staticmethod
    def get_number_nodes(ob_space, n_node_feat=None):
        if n_node_feat is None:
            n_node_feat = N_NODE_FEAT
        n_nodes = (ob_space.shape[0] - N_GLOB_FEAT) // (MAX_EDGES * (2 + N_EDGE_FEAT) + n_node_feat)
        return n_nodes

    @staticmethod
    def get_node_features(n_node_feat=None):
        if n_node_feat is None:
            return N_NODE_FEAT
        else:
            return N_NODE_FEAT

    @staticmethod
    def unpack_obs(obs, ob_space, dim_nodes=None):
        assert tf is not None, "Function unpack_obs() is not available if Tensorflow is not imported."

        if dim_nodes is None:
            dim_nodes = N_NODE_FEAT

        # assume flattened box
        n_nodes = (ob_space.shape[0] - N_GLOB_FEAT) // (MAX_EDGES * (2 + N_EDGE_FEAT) + dim_nodes)
        max_edges = MAX_EDGES
        max_n_edges = n_nodes * max_edges
        dim_edges = N_EDGE_FEAT

        # unpack node and edge data from flattened array
        # order given by self.keys = ['nodes', 'edges', 'senders', 'receivers', 'step']
        shapes = ((n_nodes, dim_nodes), (max_n_edges, dim_edges), (max_n_edges, 1), (max_n_edges, 1), (1, N_GLOB_FEAT))
        sizes = [np.prod(s) for s in shapes]
        tensors = tf.split(obs, sizes, axis=1)
        tensors = [tf.reshape(t, (-1,) + s) for (t, s) in zip(tensors, shapes)]
        nodes, edges, senders, receivers, globs = tensors

        batch_size = tf.shape(nodes)[0]
        nodes = tf.reshape(nodes, (-1, dim_nodes))
        n_node = tf.fill((batch_size,), n_nodes)  # assume n nodes is fixed

        cum_n_nodes = tf.cast(tf.reshape(tf.math.cumsum(n_node, exclusive=True), (-1, 1, 1)), dtype=tf.float32)
        senders = senders + cum_n_nodes
        receivers = receivers + cum_n_nodes

        # compute edge mask and number of edges per graph
        mask = tf.reshape(tf.not_equal(senders, -1), (batch_size, -1))  # padded edges have sender = -1
        n_edge = tf.reduce_sum(tf.cast(mask, tf.float32), axis=1)
        mask = tf.reshape(mask, (-1,))

        # flatten edge data
        edges = tf.reshape(edges, (-1, dim_edges))
        senders = tf.reshape(senders, (-1,))
        receivers = tf.reshape(receivers, (-1,))

        # mask edges
        edges = tf.boolean_mask(edges, mask, axis=0)
        senders = tf.boolean_mask(senders, mask)
        receivers = tf.boolean_mask(receivers, mask)

        globs = tf.reshape(globs, (batch_size, N_GLOB_FEAT))

        # cast all indices to int
        n_node = tf.cast(n_node, tf.int32)
        n_edge = tf.cast(n_edge, tf.int32)
        senders = tf.cast(senders, tf.int32)
        receivers = tf.cast(receivers, tf.int32)

        return batch_size, n_node, nodes, n_edge, edges, senders, receivers, globs

    @staticmethod
    def unpack_obs_state(obs, ob_space, state, dim_state, dim_nodes=None):
        assert tf is not None, "Function unpack_obs() is not available if Tensorflow is not imported."

        if dim_nodes is None:
            dim_nodes = N_NODE_FEAT

        # assume flattened box
        n_nodes = (ob_space.shape[0] - N_GLOB_FEAT) // (MAX_EDGES * (2 + N_EDGE_FEAT) + dim_nodes)
        max_edges = MAX_EDGES
        max_n_edges = n_nodes * max_edges
        dim_edges = N_EDGE_FEAT

        # unpack node and edge data from flattened array
        # order given by self.keys = ['nodes', 'edges', 'senders', 'receivers', 'step']
        shapes = ((n_nodes, dim_nodes), (max_n_edges, dim_edges), (max_n_edges, 1), (max_n_edges, 1), (1, N_GLOB_FEAT))
        sizes = [np.prod(s) for s in shapes]
        tensors = tf.split(obs, sizes, axis=1)
        tensors = [tf.reshape(t, (-1,) + s) for (t, s) in zip(tensors, shapes)]
        nodes, edges, senders, receivers, globs = tensors

        batch_size = tf.shape(nodes)[0]
        nodes = tf.reshape(nodes, (-1, dim_nodes))
        reshaped_state = tf.reshape(state, (-1, dim_state*2))
        nodes1 = tf.concat([nodes, reshaped_state[:, :dim_state]], axis=1)
        nodes2 = tf.concat([nodes, reshaped_state[:, dim_state:]], axis=1)
        n_node = tf.fill((batch_size,), n_nodes)  # assume n nodes is fixed

        cum_n_nodes = tf.cast(tf.reshape(tf.math.cumsum(n_node, exclusive=True), (-1, 1, 1)), dtype=tf.float32)
        senders = senders + cum_n_nodes
        receivers = receivers + cum_n_nodes

        # compute edge mask and number of edges per graph
        mask = tf.reshape(tf.not_equal(senders, -1), (batch_size, -1))  # padded edges have sender = -1
        n_edge = tf.reduce_sum(tf.cast(mask, tf.float32), axis=1)
        mask = tf.reshape(mask, (-1,))

        # flatten edge data
        edges = tf.reshape(edges, (-1, dim_edges))
        senders = tf.reshape(senders, (-1,))
        receivers = tf.reshape(receivers, (-1,))

        # mask edges
        edges = tf.boolean_mask(edges, mask, axis=0)
        senders = tf.boolean_mask(senders, mask)
        receivers = tf.boolean_mask(receivers, mask)

        globs = tf.reshape(globs, (batch_size, N_GLOB_FEAT))

        # cast all indices to int
        n_node = tf.cast(n_node, tf.int32)
        n_edge = tf.cast(n_edge, tf.int32)
        senders = tf.cast(senders, tf.int32)
        receivers = tf.cast(receivers, tf.int32)

        return batch_size, n_node, nodes1, nodes2, n_edge, edges, senders, receivers, globs

    def controller(self, random=False, greedy=GREEDY_CONTROLLER, reset_solution=False):
        """
        Greedy controller picks the nearest unvisited target
        :return: control action for each robot (global index of agent chosen)
        """
        if random:
            return self.np_random.choice(self.n_actions, size=(self.n_robots, 1))

        if self.graph_previous is None:
            self.graph_cost, self.graph_previous = self.construct_time_matrix()
            # print('Graph Diameter')
            # print(np.max(self.graph_cost[self.graph_cost < MAX_COST]))
            self.graph_diameter = np.max(self.graph_cost[self.graph_cost < MAX_COST])

        curr_loc = self.closest_targets

        # get nearest unvisited target
        r = self.graph_cost[curr_loc - self.n_robots, :]
        r[:, np.where(self.visited[self.n_robots:] == 1)] = MAX_COST
        if self.hide_nodes:
            r[:, np.where(np.equal(self.discovered_nodes[self.n_robots:self.n_agents], 0.0))] = MAX_COST
        greedy_loc = np.argmin(r, axis=1) + self.n_robots

        # if no unvisited targets in the horizon
        for i in range(self.n_robots):
            if r[i, greedy_loc[i] - self.n_robots] == MAX_COST:
                greedy_loc[i] = -1

        if greedy:
            next_loc = greedy_loc
        else:
            assert ortools is not None, "Vehicle routing controller is not available if OR-Tools is not imported."

            if self.cached_solution is None or self.horizon > -1 or reset_solution:
                if self.horizon > -1:
                    self.cached_solution = solve_vrp(self, min(self.horizon, EPISODE_LENGTH - self.step_counter))
                else:
                    self.cached_solution = solve_vrp(self)

            next_loc = np.zeros((self.n_robots,), dtype=int)

            for i in range(self.n_robots):

                # use the next waypoint in cached solution
                if len(self.cached_solution[i]) > 1:
                    if curr_loc[i] == self.cached_solution[i][0]:
                        self.cached_solution[i] = self.cached_solution[i][1:]

                    next_loc[i] = self.cached_solution[i][0]
                else:
                    if len(self.cached_solution[i]) == 1:
                        if curr_loc[i] == self.cached_solution[i][0]:
                            self.cached_solution[i] = []
                        else:
                            next_loc[i] = self.cached_solution[i][0]
                    else:
                        # if the cached solution is done, use the greedy solution
                        next_loc[i] = greedy_loc[i]

        u_ind = np.zeros((self.n_robots, 1), dtype=np.int32)
        for i in range(self.n_robots):

            # if the next location is unreachable with the set horizon
            if next_loc[i] == -1 or self.graph_previous[next_loc[i] - self.n_robots, curr_loc[i] - self.n_robots] == -1:
                u_ind[i] = self.np_random.choice(self.n_actions)
            else:
                # use the precomputed predecessor matrix to select the next step
                next_step = self.graph_previous[
                                next_loc[i] - self.n_robots, curr_loc[i] - self.n_robots] + self.n_robots

                u_ind[i] = np.where(self.mov_edges[1][np.where(self.mov_edges[0] == i)] == next_step)[0][0]

        return u_ind
