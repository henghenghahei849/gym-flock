import gym
import gym_flock
import numpy as np

try:
    from ortools.constraint_solver import routing_enums_pb2
    from ortools.constraint_solver import pywrapcp
    ortools = True
except ImportError:
    ortools = None

penalty_multiplier = 500


def create_data_model(env):
    """
    Formulate the vehicle routing problem corresponding to the MappingRad env to generate the expert solution
    :return: Dict containing the problem parameters
    """
    data = {}

    data['episode_length'] = env.episode_length
    init_loc = env.closest_targets - env.n_robots

    # get visitation of nodes
    need_to_visit = np.logical_not(env.visited[env.n_robots:])
    if env.hide_nodes:
        need_to_visit = np.logical_and(need_to_visit, np.not_equal(env.discovered_nodes[env.n_robots:env.n_agents], 0.0))

    penalty = need_to_visit * penalty_multiplier
    penalty = np.insert(penalty, 0, 0.0)
    data['penalties'] = penalty

    # get map edges from env
    dist_mat = np.copy(env.graph_cost)

    fill = np.ones(env.n_targets)
    fill[init_loc] = 0

    ignore = np.where(np.logical_and(env.visited[env.n_robots:].flatten(), fill))
    dist_mat[ignore, :] = penalty_multiplier
    dist_mat[:, ignore] = penalty_multiplier

    # add depot at index env.n_targets with distance = 0 to/from all nodes
    from_depot = np.ones((1, env.n_targets)) * 100000.0
    from_depot[:, init_loc] = 0.0

    to_depot = np.zeros((env.n_targets + 1, 1))

    dist_mat = np.vstack((from_depot, dist_mat))
    dist_mat = np.hstack((to_depot, dist_mat))
    data['time_matrix'] = dist_mat

    data['num_vehicles'] = env.n_robots
    data['init_loc'] = init_loc + 1
    data['depot'] = 0

    return data


def solve_vrp(env, trajectory_length=None):
    """

    :param env:
    :type env:
    :return:
    :rtype:
    """

    assert ortools is not None, "Function solve_vrp() is not available if ORTools is not imported."

    data = create_data_model(env)

    if trajectory_length is None:
        trajectory_length = int(data['episode_length'])

    # Create the routing index manager.
    manager = pywrapcp.RoutingIndexManager(len(data['time_matrix']),
                                           data['num_vehicles'], data['depot'])

    # Create Routing Model.
    routing = pywrapcp.RoutingModel(manager)

    # Create and register a transit callback.
    def time_callback(from_index, to_index):
        """Returns the distance between the two nodes."""
        # Convert from routing variable Index to distance matrix NodeIndex.
        from_node = manager.IndexToNode(from_index)
        to_node = manager.IndexToNode(to_index)
        return data['time_matrix'][from_node][to_node]

    transit_callback_index = routing.RegisterTransitCallback(time_callback)

    # Define cost of each arc.
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback_index)
    time_str = 'Time'
    routing.AddDimension(
        transit_callback_index,
        0,  # allow waiting time
        trajectory_length,  # maximum time per vehicle
        False,  # Don't force start cumul to zero.
        time_str)
    time_dimension = routing.GetDimensionOrDie(time_str)

    for i in range(data['num_vehicles']):
        routing.AddVariableMinimizedByFinalizer(
            time_dimension.CumulVar(routing.Start(i)))
        routing.AddVariableMinimizedByFinalizer(
            time_dimension.CumulVar(routing.End(i)))

    # Allow to drop nodes.
    for node in range(1, len(data['time_matrix'])):
        penalty = int(data['penalties'][manager.NodeToIndex(node)])
        routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

    # Setting first solution heuristic.
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC)

    # search_parameters.local_search_metaheuristic = (
    #     routing_enums_pb2.LocalSearchMetaheuristic.SIMULATED_ANNEALING)
    # search_parameters.time_limit.seconds = 30
    # search_parameters.first_solution_strategy = (
    #     routing_enums_pb2.FirstSolutionStrategy.CHRISTOFIDES)
    # which is the best? https://developers.google.com/optimization/routing/routing_options

    # Anytime search parameters:
    # search_parameters.time_limit.seconds = 10
    #
    # print('running solver')

    # Solve the problem.
    assignment = routing.SolveWithParameters(search_parameters)

    raw_trajectories = [[]] * data['num_vehicles']
    trajectories = [[]] * data['num_vehicles']

    for vehicle_id in range(data['num_vehicles']):
        index = assignment.Value(routing.NextVar(routing.Start(vehicle_id)))
        # index = assignment.Value(routing.Start(vehicle_id))

        # check conditions on first stops, ignore depot
        assert index in data['init_loc'], 'First stop is not an initial position'
        assert index not in [ls[0] for ls in raw_trajectories if len(ls) > 0]
        result_index = np.where(data['init_loc'] == index)[0].flatten()[0]

        result = []
        raw_result = []

        while not routing.IsEnd(index):
            cur_node = manager.IndexToNode(index)
            proc_node = cur_node - 1 + env.n_robots
            result.append(proc_node)  # remove depot indexing shift
            raw_result.append(cur_node)  # remove depot indexing shift
            index = assignment.Value(routing.NextVar(index))

        trajectories[result_index] = result
        raw_trajectories[result_index] = raw_result
        # don't add depot as last node

    return trajectories
