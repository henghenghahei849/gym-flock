import numpy as np
import matplotlib.pyplot as plt


def sample_free_space(free_region, obstacles, n_points):
    """
    Sample uniformly at random points from the free region, rejecting points that are within obstacle regions
    :param free_region: tuple (xmin, xmax, ymin, ymax)
    :param obstacles: list of rectangular obstacles [(xmin, xmax, ymin, ymax)]
    :param n_points: number of points to sample
    :return: List[(x, y)] of sampled points
    """

    # sample points from the free region
    (xmin, xmax, ymin, ymax) = free_region
    points = np.random.uniform(low=[xmin, ymin], high=[xmax, ymax], size=(n_points, 2))

    # re-sample if point within obstacle
    for i in range(n_points):
        while in_obstacle(obstacles, points[i, 0], points[i, 1]):
            points[i, :] = np.random.uniform(low=[xmin, ymin], high=[xmax, ymax], size=(1, 2))

    return points


def in_obstacle(obstacles, px, py):
    """
    Check if query point is within any of the rectangular obstacles
    :param obstacles: list of rectangular obstacles [(xmin, xmax, ymin, ymax)]
    :param px: query point x coordinate
    :param py: query point y coordinate
    :return:
    """
    for (xmin, xmax, ymin, ymax) in obstacles:
        if xmin <= px <= xmax and ymin <= py <= ymax:
            return True
    return False


def generate_lattice(free_region, lattice_vectors):
    """
    Generate hexagonal lattice
    From https://stackoverflow.com/questions/6141955/efficiently-generate-a-lattice-of-points-in-python
    :param free_region:
    :param lattice_vectors:
    :return:
    """
    (xmin, xmax, ymin, ymax) = free_region
    image_shape = np.array([xmax-xmin, ymax-ymin])
    center_pix = image_shape // 2
    # Get the lower limit on the cell size.
    dx_cell = max(abs(lattice_vectors[0][0]), abs(lattice_vectors[1][0]))
    dy_cell = max(abs(lattice_vectors[0][1]), abs(lattice_vectors[1][1]))
    # Get an over estimate of how many cells across and up.
    nx = image_shape[0] // dx_cell
    ny = image_shape[1] // dy_cell
    # Generate a square lattice, with too many points.
    x_sq = np.arange(-nx, nx, dtype=float)
    y_sq = np.arange(-ny, nx, dtype=float)
    x_sq.shape = x_sq.shape + (1,)
    y_sq.shape = (1,) + y_sq.shape
    # Now shear the whole thing using the lattice vectors
    x_lattice = lattice_vectors[0][0] * x_sq + lattice_vectors[1][0] * y_sq
    y_lattice = lattice_vectors[0][1] * x_sq + lattice_vectors[1][1] * y_sq
    # Trim to fit in box.
    mask = ((x_lattice < image_shape[0] / 2.0) & (x_lattice > -image_shape[0] / 2.0))
    mask = mask & ((y_lattice < image_shape[1] / 2.0) & (y_lattice > -image_shape[1] / 2.0))
    x_lattice = x_lattice[mask]
    y_lattice = y_lattice[mask]
    # Translate to the center pix.
    x_lattice += (center_pix[0] + xmin)
    y_lattice += (center_pix[1] + ymin)
    # Make output compatible with original version.
    out = np.empty((len(x_lattice), 2), dtype=float)
    out[:, 0] = y_lattice
    out[:, 1] = x_lattice
    return out


def reject_collisions(points, obstacles):
    """

    :param points:
    :param obstacles:
    :return:
    """
    # remove points within obstacle
    n_points = np.shape(points)[0]
    flag = np.ones((n_points,), dtype=np.bool)
    for i in range(n_points):
        if in_obstacle(obstacles, points[i, 0], points[i, 1]):
            flag[i] = False

    return points[flag, :]


if __name__ == "__main__":
    # test generate_lattice() and reject_collisions()

    # triangular lattice
    lattice_vectors = [
        3. * np.array([-1.44, -1.44]),
        3. * np.array([-1.44, 1.44])]

    # square lattice
    # lattice_vectors = [
    #     np.array([-4.0, 0.]),
    #     np.array([0., -4.0])]

    free_region = (0, 100, 0, 100)
    spots = generate_lattice(free_region, 2 * lattice_vectors)

    obstacles = [(10, 45, 10, 90), (55, 90, 10, 90)]
    spots = reject_collisions(spots, obstacles)

    plt.figure()
    plt.plot([p[1] for p in spots], [p[0] for p in spots], '.')
    plt.show()