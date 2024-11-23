import logging
import time
from typing import Callable, Iterable, Literal, Optional

import numpy as np
from docplex.mp.model import Model
from docplex.mp.solution import SolveSolution
from k_means_constrained import KMeansConstrained
from sklearn.cluster import KMeans
from tqdm import tqdm

from ev_station_solver.constants import MOPTA_CONSTANTS
from ev_station_solver.helper_functions import (
    compute_maximum_matching,
    get_distance_matrix,
    get_indice_sets_stations,
)
from ev_station_solver.location_improvement import find_optimal_location
from ev_station_solver.logging import get_logger
from ev_station_solver.solving.sample import Sample

# create logger
logger = get_logger(__name__)


class Solver:
    def __init__(
        self,
        vehicle_locations: np.ndarray,
        loglevel=logging.INFO,
        build_cost: float = MOPTA_CONSTANTS["build_cost"],
        maintenance_cost: float = MOPTA_CONSTANTS["maintenance_cost"],
        drive_cost: float = MOPTA_CONSTANTS["drive_cost"],
        charge_cost: float = MOPTA_CONSTANTS["charge_cost"],
        service_level: float = MOPTA_CONSTANTS["service_level"],
        station_ub: int = MOPTA_CONSTANTS["station_ub"],
        fixed_station_number: Optional[int] = None,
        streamlit_callback: Optional[Callable] = None,
    ):
        """
        Initialize the MOPTA solver with the given parameters.

        Common abbreviations:
        - dv: decision variable
        - lt: less than
        - cl: charging location
        - n: number

        :param vehicle_locations: the locations of the vehicles
        :param loglevel: logging level, e.g., logging.DEBUG, logging.INFO
        :param build_cost: the cost for building a location
        :param maintenance_cost: the cost for maintaining per charger at a location
        :param drive_cost: the cost per drive mile
        :param charge_cost: the cost per charged mile
        :param service_level: the percentage of vehicles that need to be charged
        :param station_ub: the number of vehicles, that can be charged at a location
        :param fixed_station_number: if wanted to specify the number of locations
        :param streamlit_callback: function to update streamlit user interface
        """
        logger.setLevel(level=loglevel)

        # Sanity checks:
        # vehicle locations are in R^2 and there are at least two vehicle locations
        if vehicle_locations.shape[0] == 1:
            raise ValueError("Please add more than one vehicle location.")
        if vehicle_locations.shape[1] != 2:
            raise ValueError("Please add two dimensional vehicle locations.")

        # Check whether service level is within (0,1]
        if service_level <= 0 or service_level > 1:
            raise ValueError("Service level should be within (0,1].")

        # set total vehicle locations
        self.vehicle_locations = vehicle_locations
        self.n_vehicles = len(self.vehicle_locations)

        # Tightest grid
        self.x_min = np.min(self.vehicle_locations[:, 0])  # most left vehicle
        self.x_max = np.max(self.vehicle_locations[:, 0])  # most right vehicle
        self.y_min = np.min(self.vehicle_locations[:, 1])  # most down vehicle
        self.y_max = np.max(self.vehicle_locations[:, 1])  # most up vehicle

        # Samples: generate all lists needed for samples
        self.samples: list[Sample] = []
        self.S: range = range(len(self.samples))

        # charging locations
        self.coordinates_potential_cl: np.ndarray = np.empty((0, 2))  # charging locations
        self.n_potential_cl: int = 0  # number of charging locations
        self.J = range(self.n_potential_cl)  # indices of potential charging locations

        # model and decision variables
        self.m = Model(name="Placement EV Chargers - Location Improvement", cts_by_name=True)  # docplex model
        self.v = np.empty((0,))  # binary decision variables whether to build cl or not
        self.w = np.empty((0,))  # integer decision variables how many to build at cl
        self.u = []  # binary decision variables for allocation per sample

        # cost terms
        self.station_ub = station_ub  # upper bound on number of stations
        self.build_cost_param = build_cost  # build cost
        self.maintenance_cost_param = maintenance_cost  # maintenance cost
        self.charge_cost_param = charge_cost  # charge cost
        self.drive_charge_cost_param = charge_cost + drive_cost  # drive + charge cost

        # objective terms (later add terms)
        self.build_cost = None
        self.maintenance_cost = None
        self.drive_charge_cost = None
        self.fixed_charge_cost = None

        # constraints #TODO: update docstrings
        self.fixed_station_number = fixed_station_number  # fixed number of stations
        self.service_level = service_level  # service level
        self.fixed_station_number_constraint = None  # fixed number of stations constraint
        self.v_lt_w_constraints = []  # n only positive if also b positive
        self.w_lt_mv_constraints = []  # b only positive if also n positive
        self.max_queue_constraints = []  # max queue length constraints
        self.allocation_constraints = []  # allocation constraints (allocated to up to one charging station)
        self.service_constraints = []  # service constraints (at least XX% are serviced)

        # kpis
        self.kpi_build = None
        self.kpi_maintenance = None
        self.kpi_drive_charge = None
        self.kpi_avg_drive_distance = None
        self.kpi_fixed_charge = None
        self.kpi_total = None

        # solutions for each iteration
        self.solutions = []  # tuples of (b_sol, n_sol, u_sol)
        self.objective_values = [np.inf]
        self.added_locations = []  # list of lists of the added locations per iteration

        # streamlit
        self.streamlit_callback = streamlit_callback  # callback function for streamlit

    def add_initial_locations(
        self,
        n_stations: int,
        mode: Literal["random", "k-means", "k-means-constrained"] = "random",
        verbose: int = 0,
        seed: Optional[int] = None,
    ) -> None:
        """
        Add initial locations to the model
        :param n_stations: number of locations to add
        :param mode: random, k-means, k-means-constrained
        :param verbose: verbosity mode
        :param seed: seed for random state
        """

        if mode == "random":
            logger.debug("Adding random locations.")
            # random generator
            rng = np.random.default_rng(seed=seed)
            # scale random locations to grid
            new_locations = rng.random((n_stations, 2)) * np.array(
                [self.x_max - self.x_min, self.y_max - self.y_min]
            ) + np.array([self.x_min, self.y_min])

        elif mode == "k-means":
            logger.debug(f"Adding {n_stations} k-means locations.")
            kmeans = KMeans(n_clusters=n_stations, n_init=1, random_state=seed, verbose=verbose)
            new_locations = kmeans.fit(self.vehicle_locations).cluster_centers_

        elif mode == "k-means-constrained":
            logger.debug(f"Adding {n_stations} k-means-constrained locations.")
            kmeans_constrained = KMeansConstrained(
                n_clusters=n_stations,
                size_max=self.station_ub
                * 2
                / MOPTA_CONSTANTS["mu_charging"],  # use expected number of charging vehicles
                n_init=1,
                random_state=seed,
                verbose=verbose,
            )
            new_locations = kmeans_constrained.fit(self.vehicle_locations).cluster_centers_

        else:
            raise Exception(
                'Invalid mode for initial locations. Choose between "random", "k-means" or "k-means-constrained".'
            )

        # add new locations
        self.coordinates_potential_cl = np.concatenate((self.coordinates_potential_cl, new_locations))

        self.n_potential_cl = len(self.coordinates_potential_cl)
        self.J = range(self.n_potential_cl)
        self.added_locations.append(self.coordinates_potential_cl)

    def add_samples(self, num: int):
        def add_sample():
            """
            Adds a sample of charging values to the problem, which is used to optimise over.
            """
            logger.debug("Adding sample.")

            if self.coordinates_potential_cl is None:
                raise Exception("Please add initial locations before adding samples.")

            sample = Sample.create_sample(
                total_vehicle_locations=self.vehicle_locations,
                coordinates_potential_cl=self.coordinates_potential_cl,
            )

            # append to samples
            self.samples.append(sample)

            # update S
            self.S = range(len(self.samples))

            # create empty u decision variables
            self.u.append(np.empty((0, sample.n_vehicles)))

        for _ in range(num):
            add_sample()
        logger.info(f"Added {num} samples. Total number of samples: {len(self.samples)}.")

    def initialize_model(self):
        # check that samples have been added
        if len(self.samples) == 0:
            raise ValueError("No samples have been added. Please add samples before solving the model.")

        # create decision variables
        logger.info("Creating decision variables...")
        self.add_new_decision_variables(K=self.J)

        # add constraints
        logger.info("Creating constraints...")
        self.update_constraints(K=self.J)

        logger.info("Model is initialized.")

    def add_new_decision_variables(self, K: Iterable):
        # logger.debug(f"We add {2 * len(K)} variables for b and n.")
        # setting new deicison variables for new potential cl

        self.add_new_dv_v(K=K)
        self.add_new_dv_w(K=K)
        for s in self.S:
            self.add_new_dv_u_s(s=s, K=K)

    def add_new_dv_v(self, K: Iterable) -> None:  # TODO: check why K is used and not J ( also below)
        """
        Create binary variables v_k for each location k in K
        :param K: Iterable of (some) locations
        :return: b
        """
        self.v = np.append(self.v, np.array([self.m.binary_var(name=f"v_{k}") for k in K]))

    def add_new_dv_w(self, K: Iterable) -> None:
        """
        Create integer variables n_k for each location k in K
        :param K: Iterable of (some) locations
        :return: n
        """
        self.w = np.append(self.w, np.array([self.m.integer_var(name=f"w_{k}") for k in K]))

    def add_new_dv_u_s(self, s: int, K: Iterable):
        created_u_s = np.array(
            [
                self.m.binary_var(name=f"u_{s}_{i}_{k}") if self.samples[s].reachability_matrix[i, k] else 0
                for i in self.samples[s].I
                for k in K
            ]
        )
        created_u_s = created_u_s.reshape(self.samples[s].n_vehicles, len(K))
        self.u[s] = np.concatenate((self.u[s], created_u_s), axis=1)

    def update_constraints(self, K: Iterable):
        if self.fixed_station_number is not None:
            self.update_fixed_station_number_constraint(K=K)

        self.v_lt_w_constraints = self.add_w_lt_mv_constraints(K=K)
        self.w_lt_mv_constraints = self.add_v_lt_w_constraints(K=K)

        for s in self.S:
            self.add_max_queue_constraints(s=s, K=K)
            self.add_allocation_constraints(s=s, K=K)
            self.update_service_constraint(s=s, K=K)
            self.update_allocation_constraints(s, K=K)


    def update_fixed_station_number_constraint(self, K: Iterable):
        # set constrained of fixed number of built chargers
        constraint = self.m.get_constraint_by_name("fixed_station_number")

        if constraint is None:
            self.fixed_station_number_constraint = self.m.add_constraint(
                self.m.sum(self.v) == self.fixed_station_number, ctname="fixed_station_number"
            )
        else:
            self.fixed_station_number_constraint = constraint.left_expr.add(self.m.sum(self.v[k] for k in K))

    def add_w_lt_mv_constraints(self, K: Iterable):
        logger.debug("Adding 'w <= v * station_upperbound' constraints.")
        new_w_lt_mv_constraints = self.m.add_constraints(
            (self.w[k] <= self.v[k] * self.station_ub for k in K),
            names=(f"number_w_{k}" for k in K),
        )
        self.w_lt_mv_constraints += new_w_lt_mv_constraints

    def add_v_lt_w_constraints(self, K: Iterable):
        logger.debug("Adding 'b <= n' constraints.")  # TODO: update docstrings and logging
        new_v_lt_mv_constraints = self.m.add_constraints(
            (self.v[k] <= self.w[k] for k in K), names=(f"number_v_{k}" for k in K)
        )
        self.v_lt_w_constraints += new_v_lt_mv_constraints

    def add_max_queue_constraints(self, s: int, K: Iterable, q: int = MOPTA_CONSTANTS["queue_size"]):
        logger.debug("Adding max queue constraints (allocated vehicles <= max queue).")
        new_max_queue_constraints = self.m.add_constraints(
            (
                self.m.sum(self.u[s][i, k] for i in self.samples[s].I if self.samples[s].reachability_matrix[i, k])
                <= q * self.w[k]
                for k in K
            ),
            names=(f"allocation_qw_{s}_{k}" for k in K),
        )
        self.max_queue_constraints += new_max_queue_constraints


        # else:
        #     self.v_lt_w_constraints += self.add_w_lt_mv_constraints(K=K)
        #     self.w_lt_mv_constraints += self.add_v_lt_w_constraints(K=K)
        #     for s in self.samples_range:
        #         self.max_queue_constraints[s] += self.add_max_queue_constraints(s=s, K=K)
        #         self.service_constraints[s] = self.m.get_constraint_by_name(f"service_level_{s}").left_expr.add(
        #             self.m.sum(
        #                 self.u[s][i, k]
        #                 for i in self.samples[s].I
        #                 for k in K
        #                 if self.samples[s].reachability_matrix[i, k]
        #             )
        #         )
        #         for i in self.samples[s].I:
        #             self.allocation_constraints[s][i] = self.m.get_constraint_by_name(
        #                 f"charger_allocation_{s}_{i}"
        #             ).left_expr.add(self.m.sum(self.u[s][i, k] for k in K if self.samples[s].reachability_matrix[i, k]))

    def add_allocation_constraints(self, s: int, K: Iterable):
        logger.debug("Adding allocation constraints (every vehicle is allocated to at most one station).")
        return self.m.add_constraints(
            (
                self.m.sum(self.u[s][i, k] for k in K if self.samples[s].reachability_matrix[i, k]) <= 1
                for i in self.samples[s].I
            ),
            names=(f"charger_allocation_{s}_{i}" for i in self.samples[s].I),
        )

    def update_service_constraint(self, s: int, K: Iterable):
        logger.debug(f"Adding service constraint (min. {self.service_level * 100}% of vehicles are allocated).")
        return self.m.add_constraint(
            (
                self.m.sum(
                    self.u[s][i, k] for i in self.samples[s].I for k in K if self.samples[s].reachability_matrix[i, k]
                )
                >= self.service_level * self.samples[s].n_vehicles
            ),
            ctname=f"service_level_{s}",
        )

    def get_build_cost(self, K: Iterable):
        return self.build_cost_param * self.m.sum(self.v[k] for k in K)

    def get_maintenance_cost(self, K: Iterable):
        return self.maintenance_cost_param * self.m.sum(self.w[k] for k in K)

    def get_drive_charge_cost(self, s: int, K: Iterable):
        return self.drive_charge_cost_param * self.m.sum(
            self.u[s][i, k] * self.samples[s].distance_matrix[i, k]
            for i in self.samples[s].I
            for k in K
            if self.samples[s].reachability_matrix[i, k]
        )

    def get_fixed_charge_cost(self, s: int):
        return self.charge_cost_param * (250 - self.samples[s].ranges).sum()

    def extract_solution(self, sol: SolveSolution, dtype=float):
        logger.info("Extracting solution.")
        b_sol = np.array(sol.get_value_list(dvars=self.v)).round().astype(dtype)
        n_sol = np.array(sol.get_value_list(dvars=self.w)).round().astype(dtype)

        u_sol = []
        for s in self.samples_range:
            u_sol.append(np.zeros(self.u[s].shape))
            u_sol[s][self.samples[s].reachability_matrix[i, k]] = np.array(
                sol.get_value_list(dvars=self.u[s][self.samples[s].reachability_matrix[i, k]].flatten())
            )
            u_sol[s] = u_sol[s].round().astype(dtype)
        # round needed for numerical stability (e.g. solution with 0.9999999999999999)
        return b_sol, n_sol, u_sol

    def filter_locations(
        self,
        improved_locations: np.ndarray,
        old_location_indices: np.ndarray,
        min_distance: float = MOPTA_CONSTANTS["min_distance"],
        counting_radius: float = MOPTA_CONSTANTS["counting_radius"],
    ):
        distances = get_distance_matrix(improved_locations, self.coordinates_potential_cl).min(axis=1)
        build_mask = distances > min_distance
        too_close = np.argwhere(~build_mask).flatten()

        if len(too_close) == 0:
            logger.debug("No locations are too close to other locations. No filtering needed.")
            return improved_locations, old_location_indices
        else:
            # compute distances to all vehicles and compute how many are in radius
            distances_vehicles = get_distance_matrix(improved_locations[too_close], self.vehicle_locations)
            number_vehicles_in_radius = (distances_vehicles < counting_radius).sum(axis=1) * MOPTA_CONSTANTS[
                "mu_charging"
            ]  # multiply by expected charging prob

            # compute number of chargers in radius and how many are in radius

            distances_chargers = get_distance_matrix(improved_locations[too_close], self.coordinates_potential_cl)
            number_locations_radius = (distances_chargers < counting_radius).sum(axis=1) * 2 * self.station_ub

            # compute probability of adding a new one location
            # print(number_locations_radius)
            prob = np.zeros(len(number_locations_radius))
            for i in range(len(number_locations_radius)):
                if number_locations_radius[i] == 0:
                    prob[i] = 1
                else:
                    prob[i] = number_vehicles_in_radius[i] / number_locations_radius[i]

            build_mask[too_close] = np.random.uniform(size=len(too_close)) < prob
            logger.debug(f"The probabilities for building of chargers that are too close to others are {prob}.")
            return improved_locations[build_mask], old_location_indices[build_mask]

    def find_improved_locations(self, built_indices: np.ndarray, u_sol: list):
        # create lists for improved locations and their old indices (used for warmstart)
        improved_locations = []
        location_indices = []
        empty_indices = []

        for j in tqdm(built_indices):
            # find allocated vehicles and their ranges
            X_allocated = []
            ranges_allocated = []
            for s in self.samples_range:
                indices_vehicles_s = np.argwhere(
                    u_sol[s][:, j] == 1
                ).flatten()  # indices of allocated vehicles to specific charger
                X_allocated.append(self.vehicle_locations_matrices[s][indices_vehicles_s])
                ranges_allocated.append(self.samples[s].ranges[indices_vehicles_s])

            # combine them
            X_allocated = np.vstack(X_allocated)  # combine all vehicle locations from the different samples
            ranges_allocated = np.hstack(ranges_allocated)  # same for the ranges

            if len(X_allocated) != 0:  # if more than zero vehicles allocated to built charger
                # append new locations
                optimal_location = find_optimal_location(
                    allocated_locations=X_allocated, allocated_ranges=ranges_allocated
                )
                distance_old = np.linalg.norm(optimal_location - self.coordinates_potential_cl[j])
                # move slightly if really close to old chager
                if distance_old < 10e-2:
                    optimal_location += np.random.normal(scale=0.3, size=2)

                improved_locations.append(optimal_location)
                location_indices.append(j)
            else:
                # charger is built bot no vehicles are allocated
                empty_indices.append(j)

        # convert lists to numpy arrays
        improved_locations = np.array(improved_locations)
        location_indices = np.array(location_indices)
        empty_indices = np.array(empty_indices)

        return improved_locations, location_indices, empty_indices

    def set_objective(self, K: Iterable):
        # sanity check: all of them are None or all of them are not None
        if (self.build_cost is None) != (self.maintenance_cost is None) != (self.drive_charge_cost is None):
            raise ValueError("All of build_cost, maintenance_cost and drive_charge_cost must be None or not None.")

        if self.build_cost is None:  # then all of them are None
            self.build_cost = self.get_build_cost(K=K)
            self.maintenance_cost = self.get_maintenance_cost(K=K)
            self.drive_charge_cost = sum(self.get_drive_charge_cost(s=s, K=K) for s in self.samples_range)
            self.fixed_charge_cost = sum(
                self.get_fixed_charge_cost(s=s) for s in self.samples_range
            )  # independent of K
        else:
            self.build_cost += self.get_build_cost(K=K)
            self.maintenance_cost += self.get_maintenance_cost(K=K)
            self.drive_charge_cost += sum(self.get_drive_charge_cost(s=s, K=K) for s in self.samples_range)

        # set objective
        self.m.minimize(
            self.build_cost
            + self.maintenance_cost
            + 365 / self.n_samples * self.drive_charge_cost
            + 365 / self.n_samples * self.fixed_charge_cost
        )
        logger.debug("Objective set.")

    def check_stable(self, warmstart, epsilon: float = 10e-2):
        objective_warmstart = self.m.kpi_value_by_name(name="total_cost", solution=warmstart)
        if abs(self.objective_values[-1] - objective_warmstart) <= epsilon:
            return True
        else:
            return False

    def update_distances_reachable(self, v: int, improved_locations: np.ndarray, K: Iterable):
        for s in self.samples_range:
            self.distance_matrices[s] = np.concatenate(
                (
                    self.distance_matrices[s],
                    get_distance_matrix(self.vehicle_locations_matrices[s], improved_locations),
                ),
                axis=1,
            )  # add new distances
            new_reachable = np.array(
                [
                    self.samples[s].distance_matrix[i, k] <= self.samples[s].ranges[i]
                    for i in self.samples[s].I
                    for k in K
                ]
            ).reshape(self.n_vehicles_samples[s], v)
            self.samples[s].reachability_matrix[i, k] = np.concatenate(
                (self.samples[s].reachability_matrix[i, k], new_reachable), axis=1
            )

    def construct_mip_start(
        self,
        u_sol: list,
        b_sol: np.ndarray,
        n_sol: np.ndarray,
        location_indices: np.ndarray,
        empty_indices: np.ndarray,
        v: int,
        K: Iterable,
    ):
        b_start = np.concatenate((b_sol, np.zeros(v, dtype=float)))
        n_start = np.concatenate((n_sol, np.zeros(v, dtype=float)))
        u_start = []
        for s in self.samples_range:
            u_start.append(
                np.concatenate((u_sol[s], np.zeros((self.n_vehicles_samples[s], v), dtype=float)), axis=1, dtype=float)
            )

        # set new locations to built and copy their old n value
        b_start[K] = 1
        n_start[K] = n_sol[location_indices]
        # set old locations to not built
        b_start[location_indices] = 0
        n_start[location_indices] = 0
        # update u
        for s in self.samples_range:
            for k, j in enumerate(location_indices):
                indices_vehicles = np.argwhere(u_sol[s][:, j] == 1).flatten()
                for i in indices_vehicles:
                    u_start[s][i, j] = 0
                    u_start[s][i, K[k]] = 1
                    if not self.samples[s].reachability_matrix[i, k][i, K[k]]:
                        logger.warning(f"vehicle {i} cannot reach location {K[k]}")

        # check whether there are built locations that are empty
        if len(empty_indices) > 0:
            logger.info(f"Found {len(empty_indices)} built locations with no vehicles allocated ->set them to 0.")
            for i in empty_indices:
                b_start[i] = 0
                n_start[i] = 0

        # construct the MIP start with the arrays computed above
        mip_start = self.m.new_solution()
        # name solution
        mip_start.name = "warm start"
        for j in self.J:
            if b_start[j] == 1:
                if n_start[j] == 0:
                    logger.warning("Built location with n=0.")
                    continue  # skip built locations with n=0, because b should be set to 0 then

                mip_start.add_var_value(self.v[j], b_start[j])
                mip_start.add_var_value(self.w[j], n_start[j])
        for s in self.samples_range:
            for u_dv, u_val in zip(
                self.u[s][self.samples[s].reachability_matrix[i, k]],
                u_start[s][self.samples[s].reachability_matrix[i, k]],
            ):
                if u_val == 1:
                    mip_start.add_var_value(u_dv, u_val)

        return mip_start, b_start, n_start, u_start

    def set_kpis(self):
        if self.kpi_total is not None:
            self.m.remove_kpi(self.kpi_total)
        if self.kpi_build is not None:
            self.m.remove_kpi(self.kpi_build)
        if self.kpi_maintenance is not None:
            self.m.remove_kpi(self.kpi_maintenance)
        if self.kpi_drive_charge is not None:
            self.m.remove_kpi(self.kpi_drive_charge)
        if self.kpi_fixed_charge is not None:
            self.m.remove_kpi(self.kpi_fixed_charge)

        # add new kpis
        self.kpi_total = self.m.add_kpi(
            self.build_cost
            + self.maintenance_cost
            + 365 / self.n_samples * self.drive_charge_cost
            + 365 / self.n_samples * self.fixed_charge_cost,
            "total_cost",
        )
        self.kpi_build = self.m.add_kpi(self.build_cost, "build_cost")
        self.kpi_maintenance = self.m.add_kpi(self.maintenance_cost, "maintenance_cost")
        self.kpi_drive_charge = self.m.add_kpi(365 / self.n_samples * self.drive_charge_cost, "drive_charge_cost")
        self.kpi_fixed_charge = self.m.add_kpi(365 / self.n_samples * self.fixed_charge_cost, "fixed_charge_cost")

        logger.debug("KPIs set.")

    def report_kpis(
        self, solution, kpis=["total_cost", "fixed_charge_cost", "build_cost", "maintenance_cost", "drive_charge_cost"]
    ):
        logger.info(f"KPIs {solution.name}:")
        for kpi in kpis:
            logger.info(f"  - {kpi}: {round(self.m.kpi_value_by_name(name=kpi, solution=solution), 2)}")

    def solve(
        self,
        epsilon_stable: float = 10e-2,
        counting_radius: float = MOPTA_CONSTANTS["counting_radius"],
        min_distance: float = MOPTA_CONSTANTS["min_distance"],
        timelimit: float = 10,
        verbose: bool = False,
    ):
        """
        Solves the optimization problem for EV charger placement.

        Parameters:
            epsilon_stable (float): The threshold for determining stability of the solution. Defaults to 10e-2.
            counting_radius (float): The radius within which locations are counted. Defaults to MOPTA_CONSTANTS["counting_radius"].
            min_distance (float): The minimum distance between built and not built locations. Defaults to MOPTA_CONSTANTS["min_distance"].
            timelimit (float): the maximum allowable time in seconds between successive solutions in the branch-and-cut tree. Defaults to 0.5s
            verbose (bool): Whether to log detailed output during the optimization routine. Defaults to False.

        Raises:
            ValueError: If the number of fixed locations is larger than the number of available locations.
            ValueError: If the service level cannot be reached with the given number of locations.
            ValueError: If the model is infeasible.

        Returns:
            None
        """

        # sanity check for at least the number of fixed locations
        if self.fixed_station_number is not None and self.fixed_station_number > self.n_potential_cl:
            raise ValueError(
                "Number of fixed locations is larger than the number of available locations. "
                "Please add more locations."
            )

        # compute all maximum service levels to check for infeasibility
        max_service_levels = [
            compute_maximum_matching(
                n=np.repeat(8, self.n_potential_cl), reachable=self.samples[s].reachability_matrix[i, k]
            )
            for s in self.samples_range
        ]
        if min(max_service_levels) < self.service_level:
            raise ValueError(
                "Service level cannot be reached with the given number of locations. " "Please add more locations."
            )
        logger.debug(f"Maximum service levels for samples: {max_service_levels}")

        # monitor time
        start_time = time.time()

        # monitor number of iterations
        iterations = 0

        # initialize model, set objective and kpi's
        self.initialize_model()
        self.set_objective(K=self.J)
        self.set_kpis()

        # set solve parameters
        self.m.parameters.preprocessing.presolve = 0  # turn presolve off to avoid issues after lcoation improvement
        self.m.parameters.mip.limits.solutions = 1  # stop after every found solution

        # start the optimization routine
        logger.info("Starting the optimization routine.")
        # optimise the model without a time limit to get a feasible starting solution
        sol = self.m.solve(log_output=verbose, clean_before_solve=False)
        if self.m.solve_details.status == "integer infeasible":
            raise ValueError(
                "Model is infeasible. Please add more initial locations and / or "
                "increase fixed number of chargers (if fixed)."
            )

        # If it is feasible, then solution is found and we can continue from there
        logger.info("First feasible solution is found.")

        # set timelimit per improvement iteration for future solves
        if timelimit is not None:
            self.m.parameters.timelimit.set(timelimit)

        while True:
            # This while loop runs until either
            # - the objective value does not increase
            # - no improved location is found

            # update iterations
            iterations += 1

            # count inner iterations for logging and potential future improvement tracking
            inner_iteration_counter = 0
            b_start_defined = False
            while True:  # run this while improvement is good enough
                inner_iteration_counter += 1
                old_objective_value = sol.objective_value  # get the value of the current solution

                # solve the model and get the status of the solution
                sol = self.m.solve(log_output=verbose, clean_before_solve=False)
                status = self.m.solve_details.status  # status of the solution
                obj_value = self.m.objective_value  # objective value of found solution

                if status == "solution limit exceeded":
                    improvement = round(old_objective_value - obj_value, 2)  # improvement in objective value

                    logger.debug(f"Solution found, which is ${improvement} better. Continue with the next iteration.")

                    # logg infor all 4 iterations
                    if inner_iteration_counter % 4 == 0:
                        logger.info(f"Improving the current solution. Current objective value: ${round(obj_value, 2)}")
                    # keep going since the solution limit was exceeded
                    continue

                # if the model is infeasible then most likely the service level is too high for current locations
                # -> add more locations
                elif status == "integer infeasible":
                    raise ValueError(
                        "Model is infeasible. Please add more initial locations and / or "
                        "increase fixed number of chargers (if fixed)."
                    )

                elif status == "time limit exceeded":
                    # Since no improvement was found in the set time we continue with the location improvement
                    logger.info("Time limit exceeded. Continue with location improvement.")
                    break

                elif status == "integer optimal, tolerance":
                    # if an optimal solution is found we can proceed with the location improvement
                    logger.info("Optimal solution found. Continue with location improvement.")
                    break
                else:
                    logger.warning(f"Status: {status}.")
                    break

            # extract current solution
            sol.name = "CPLEX solution"  # name solution for KPI reporting
            b_sol, n_sol, u_sol = self.extract_solution(sol=sol, dtype=int)
            self.solutions.append((b_sol, n_sol, u_sol))  # append solution vectors to list of solutions
            self.objective_values.append(sol.objective_value)  # append objective value of current solution

            # if a streamlit callback function was added -> call it
            if self.streamlit_callback is not None:
                self.streamlit_callback(self)

            # determine which stations are built to improve their location
            built_indices, not_built_indices = get_indice_sets_stations(b_sol)
            logger.debug(f"There are {len(built_indices)} built and {len(not_built_indices)} not built locations.")
            # compute for every built location its best location. Return that location and its indice
            improved_locations, location_indices, empty_indices = self.find_improved_locations(
                built_indices=built_indices, u_sol=u_sol
            )

            # filter locations that are built within a distance of a not built location
            filtered_improved_locations, filtered_old_indices = self.filter_locations(
                improved_locations=improved_locations,
                old_location_indices=location_indices,
                min_distance=min_distance,
                counting_radius=counting_radius,
            )

            # if no new locations found
            v = len(filtered_improved_locations)
            if v == 0:
                logger.info("No new locations found -> stopping the optimization routine.")
                break

            # add improved locations
            self.added_locations.append(filtered_improved_locations)

            # update problem
            K = range(self.n_potential_cl, self.n_potential_cl + v)  # range for new locations
            self.coordinates_potential_cl = np.concatenate(
                (self.coordinates_potential_cl, filtered_improved_locations)
            )  # update locations

            # update distances and reachable
            self.update_distances_reachable(v=v, improved_locations=filtered_improved_locations, K=K)

            # Update number of locations and location range
            self.n_potential_cl += v
            self.J = range(self.n_potential_cl)
            logger.info(
                f"{len(filtered_improved_locations)} improved new locations found. There are now {self.n_potential_cl} locations."
            )

            # update new decision variables
            logger.info("Updating decision variables.")
            self.add_new_decision_variables(K=K)

            # update the problem and resolve
            logger.info("Updating constraints.")
            self.update_constraints(K=K)

            ## Update Objective Function Constituent Parts. Note that the charge_cost doesn't change
            logger.info("Updating objective function.")
            self.set_objective(K=K)

            # update kpis
            self.set_kpis()

            # generate new mip start
            # generate start vector for new solution
            mip_start, b_start, n_start, u_start = self.construct_mip_start(
                u_sol=u_sol,
                b_sol=b_sol,
                n_sol=n_sol,
                location_indices=filtered_old_indices,
                empty_indices=empty_indices,
                v=v,
                K=K,
            )

            b_start_defined = True
            # Add mipstart
            self.m.add_mip_start(mip_start, complete_vars=True, effort_level=4, write_level=3)
            # report both solutions
            self.report_kpis(solution=sol)
            self.report_kpis(solution=mip_start)

            # check if solution is stable -> There was no improvement compare to the last iteration
            # If it is stop the algorithm
            if self.check_stable(epsilon=epsilon_stable, warmstart=mip_start):
                logger.info("Solution is stable -> stopping the optimization routine.")
                break
        self.build_cost_sol = self.maintenance_cost_param * np.sum(n_sol) + self.build_cost_param * np.sum(b_sol)
        self.drive_cost_sol = (
            self.objective_values[-1] - 365 / self.n_samples * self.fixed_charge_cost - self.build_cost_sol
        )
        # clear model to free resources
        self.m.end()

        # cast b_start and n_start to int since they are not longer needed to be floats for warmstarts
        b_start = b_start.astype("int")
        n_start = n_start.astype("int")

        end_time = time.time()

        # Always return the solution with the optimised locations (we don't vehiclee how close)
        # compute the best locations without filtering
        logger.info("Computing improved locations without filtering for minimum distance for current allocations.")
        locations_built, _, _ = self.find_improved_locations(
            built_indices=np.argwhere(b_start == 1).flatten(), u_sol=u_start
        )
        v_sol_built = n_start[b_start == 1]

        logger.info(f"Optimization finished in {round(end_time - start_time, 2)} seconds.")
        logger.info(f"There are {b_start.sum()} built locations with in total {n_start.sum()} chargers.")

        # obtain mip gaps
        mip_gap = self.m.solve_details.gap
        mip_gap_relative = self.m.solve_details.mip_relative_gap

        return v_sol_built, locations_built, mip_gap, mip_gap_relative, iterations

    def allocation_problem(
        self,
        locations_built: np.ndarray,
        v_sol_built: np.ndarray,
        verbose: bool = False,
        n_iter: int = 50,
        timelimit: int = 60,
    ):
        # initialize model
        objective_values = []  # objective values of all solutions
        build_cost = []
        distance_cost = []
        service_levels = []  # service levels of all solutions
        mip_gaps = []  # mip gaps of all solutions

        build_maintenance_term = self.maintenance_cost_param * np.sum(v_sol_built) + self.build_cost_param * len(
            v_sol_built
        )

        w = len(locations_built)
        J = range(w)
        logger.info(f"Starting allocation problem with {n_iter} iterations.")

        # Create model once and then update it
        m_a = Model("Allocation Problem")
        expected_number_vehicles = int(self.n_vehicles * MOPTA_CONSTANTS["mu_charging"])

        logger.info("Creating decision variables")
        # create a general u for the expexted number of vehicles
        u = np.array([m_a.binary_var(name=f"u_{i}_{j}") for i in range(expected_number_vehicles) for j in J]).reshape(
            expected_number_vehicles, w
        )

        logger.info("Decision variables added.")

        # since some decision variables in some samples have no effect -> turn off presolve
        m_a.parameters.preprocessing.presolve = 0
        # set time limit
        m_a.parameters.timelimit.set(timelimit)

        for i in range(n_iter):
            logger.info(f"Allocation iteration {i + 1}/{n_iter}.")
            # clear all constraints from the previous iteration
            m_a.clear_constraints()

            # sample one sample
            ranges, charging_prob, charging = self.get_sample()
            logger.debug("  - Sample generated.")

            # filter for vehicles that are charging
            ranges = ranges[charging]
            locations = self.vehicle_locations[charging]
            distances = get_distance_matrix(locations, locations_built)
            reachable = (distances.T <= ranges).T

            # compute attainable service level
            logger.debug("  - Checking what service level is attainable.")
            attainable_service_level = compute_maximum_matching(n=v_sol_built, reachable=reachable)
            service_level = (
                self.service_level if attainable_service_level >= self.service_level else attainable_service_level
            )

            logger.debug(
                f"  - Attainable service level: {round(attainable_service_level * 100, 2)}% "
                f"(set to {round(service_level * 100, 2)})"
            )

            # set up ranges for problem
            l = charging.sum()
            I = range(l)

            # check if size of u is sufficient: if not -> extend u
            if l > u.shape[0]:
                # append decision variables onto u
                size = l - u.shape[0]
                new_u = np.array([m_a.binary_var(name=f"u_{i}_{j}") for i in range(l - size, l) for j in J]).reshape(
                    size, w
                )
                u = np.concatenate((u, new_u), axis=0)

            u_reachable = np.where(reachable, u[:l, :], 0)  # define u for this sample

            # Add constraints to it
            logger.debug("  - Setting the allocation constraints.")
            m_a.add_constraints((m_a.sum(u_reachable[i, j] for j in J) <= 1 for i in I))  # allocated up to one charger

            logger.debug("  - Setting the 2 * n constraints.")
            m_a.add_constraints(
                (m_a.sum(u_reachable[i, j] for i in I) <= 2 * v_sol_built[j] for j in J)
            )  # allocated up to 2n

            logger.debug(f"  - Setting the service level constraint to {round(service_level * 100, 2)}%.")
            m_a.add_constraint(m_a.sum(u_reachable) / l >= service_level)

            logger.debug("  - Setting the objective function for the distance minimisation.")
            constant_term = self.charge_cost_param * 365 * (250 - ranges).sum()
            m_a.minimize(
                365 * self.drive_charge_cost_param * m_a.sum(u_reachable * distances)
                + build_maintenance_term
                + constant_term
            )

            logger.debug("  - Starting the solve process.")
            sol = m_a.solve(log_output=verbose, clean_before_solve=True)

            # report objective values
            objective_value = sol.objective_value
            logger.debug(f"  - Objective value: ${round(objective_value, 2)}")
            logger.debug(f"  - Build cost: ${round(build_maintenance_term, 2)}")
            logger.debug(f"  - Constant term: ${round(constant_term, 2)}")
            logger.debug(f"  - Distance cost: ${round(objective_value - constant_term - build_maintenance_term, 2)}")

            # add values to lists
            objective_values.append(sol.objective_value)
            build_cost.append(build_maintenance_term)
            distance_cost.append(objective_value - constant_term - build_maintenance_term)
            service_levels.append(service_level)
            mip_gaps.append(m_a.solve_details.gap)

        # Clear model to free resources
        m_a.end()

        # convert to numpy arrays
        objective_values = np.array(objective_values)
        service_levels = np.array(service_levels)
        mip_gaps = np.array(mip_gaps)

        i_infeasible = np.argwhere(service_levels < self.service_level).flatten()
        feasible = np.argwhere(service_levels >= self.service_level).flatten()

        # Result logging
        logger.info(f"Out of {n_iter} samples, {len(feasible)} are feasible.")
        # check that lists are actually not empty
        if len(feasible) != 0:
            logger.info(f"- Mean objective value (feasible): ${np.round(np.mean(objective_values[feasible]), 2)}.")
        if len(i_infeasible) != 0:
            logger.info(
                f"- Mean objective value (infeasible): ${np.round(np.mean(objective_values[i_infeasible]), 2)} with a mean service level "
                f"of {np.round(np.mean(service_levels[i_infeasible]) * 100, 2)}%."
            )

        return objective_values, build_cost, distance_cost, service_levels, mip_gaps
