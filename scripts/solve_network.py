# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: AGPL-3.0-or-later

# -*- coding: utf-8 -*-
"""
Solves linear optimal power flow for a network iteratively while updating
reactances.

Relevant Settings
-----------------

.. code:: yaml

    solving:
        tmpdir:
        options:
            formulation:
            clip_p_max_pu:
            load_shedding:
            noisy_costs:
            nhours:
            min_iterations:
            max_iterations:
            skip_iterations:
            track_iterations:
        solver:
            name:

.. seealso::
    Documentation of the configuration file ``config.yaml`` at
    :ref:`electricity_cf`, :ref:`solving_cf`, :ref:`plotting_cf`

Inputs
------

- ``networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc``: confer :ref:`prepare`

Outputs
-------

- ``results/networks/elec_s{simpl}_{clusters}_ec_l{ll}_{opts}.nc``: Solved PyPSA network including optimisation results

    .. image:: /img/results.png
        :width: 40 %

Description
-----------

Total annual system costs are minimised with PyPSA. The full formulation of the
linear optimal power flow (plus investment planning)
is provided in the
`documentation of PyPSA <https://pypsa.readthedocs.io/en/latest/optimal_power_flow.html#linear-optimal-power-flow>`_.
The optimization is based on the ``pyomo=False`` setting in the :func:`network.lopf` and  :func:`pypsa.linopf.ilopf` function.
Additionally, some extra constraints specified in :mod:`prepare_network` are added.

Solving the network in multiple iterations is motivated through the dependence of transmission line capacities and impedances on values of corresponding flows.
As lines are expanded their electrical parameters change, which renders the optimisation bilinear even if the power flow
equations are linearized.
To retain the computational advantage of continuous linear programming, a sequential linear programming technique
is used, where in between iterations the line impedances are updated.
Details (and errors made through this heuristic) are discussed in the paper

- Fabian Neumann and Tom Brown. `Heuristics for Transmission Expansion Planning in Low-Carbon Energy System Models <https://arxiv.org/abs/1907.10548>`_), *16th International Conference on the European Energy Market*, 2019. `arXiv:1907.10548 <https://arxiv.org/abs/1907.10548>`_.

.. warning::
    Capital costs of existing network components are not included in the objective function,
    since for the optimisation problem they are just a constant term (no influence on optimal result).

    Therefore, these capital costs are not included in ``network.objective``!

    If you want to calculate the full total annual system costs add these to the objective value.

.. tip::
    The rule :mod:`solve_all_networks` runs
    for all ``scenario`` s in the configuration file
    the rule :mod:`solve_network`.
"""
import logging
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa
from _helpers import configure_logging, create_logger
from linopy import merge
from pypsa.descriptors import get_switchable_as_dense as get_as_dense
from pypsa.optimization.abstract import optimize_transmission_expansion_iteratively
from pypsa.optimization.optimize import optimize
from vresutils.benchmark import memory_logger

logger = create_logger(__name__)


def prepare_network(n, solve_opts, config=None):
    if "clip_p_max_pu" in solve_opts:
        for df in (n.generators_t.p_max_pu, n.storage_units_t.inflow):
            df.where(df > solve_opts["clip_p_max_pu"], other=0.0, inplace=True)

    load_shedding = solve_opts.get("load_shedding")
    if load_shedding:
        n.add("Carrier", "Load")
        buses_i = n.buses.query("carrier == 'AC'").index
        if not np.isscalar(load_shedding):
            load_shedding = 8e3  # Eur/kWh
        # intersect between macroeconomic and surveybased
        # willingness to pay
        # http://journal.frontiersin.org/article/10.3389/fenrg.2015.00055/full)
        # 1e2 is practical relevant, 8e3 good for debugging
        n.madd(
            "Generator",
            buses_i,
            " load",
            bus=buses_i,
            carrier="load",
            sign=1e-3,  # Adjust sign to measure p and p_nom in kW instead of MW
            marginal_cost=load_shedding,
            p_nom=1e9,  # kW
        )

    if solve_opts.get("noisy_costs"):
        for t in n.iterate_components(n.one_port_components):
            # TODO: uncomment out to and test noisy_cost (makes solution unique)
            # if 'capital_cost' in t.df:
            #    t.df['capital_cost'] += 1e1 + 2.*(np.random.random(len(t.df)) - 0.5)
            if "marginal_cost" in t.df:
                t.df["marginal_cost"] += 1e-2 + 2e-3 * (
                    np.random.random(len(t.df)) - 0.5
                )

        for t in n.iterate_components(["Line", "Link"]):
            t.df["capital_cost"] += (
                1e-1 + 2e-2 * (np.random.random(len(t.df)) - 0.5)
            ) * t.df["length"]

    if solve_opts.get("nhours"):
        nhours = solve_opts["nhours"]
        n.set_snapshots(n.snapshots[:nhours])
        n.snapshot_weightings[:] = 8760.0 / nhours

    return n


def add_CCL_constraints(n, config):
    agg_p_nom_limits = config["electricity"].get("agg_p_nom_limits")

    try:
        agg_p_nom_minmax = pd.read_csv(agg_p_nom_limits, index_col=list(range(2)))
    except IOError:
        logger.exception(
            "Need to specify the path to a .csv file containing "
            "aggregate capacity limits per country in "
            "config['electricity']['agg_p_nom_limit']."
        )
    logger.info(
        "Adding per carrier generation capacity constraints for " "individual countries"
    )

    gen_country = n.generators.bus.map(n.buses.country)
    capacity_variable = n.model["Generator-p_nom"]

    lhs = []
    ext_carriers = n.generators.query("p_nom_extendable").carrier.unique()
    for c in ext_carriers:
        ext_carrier = n.generators.query("p_nom_extendable and carrier == @c")
        country_grouper = (
            ext_carrier.bus.map(n.buses.country)
            .rename_axis("Generator-ext")
            .rename("country")
        )
        ext_carrier_per_country = capacity_variable.loc[
            country_grouper.index
        ].groupby_sum(country_grouper)
        lhs.append(ext_carrier_per_country)
    lhs = merge(lhs, dim=pd.Index(ext_carriers, name="carrier"))

    min_matrix = agg_p_nom_minmax["min"].to_xarray().unstack().reindex_like(lhs)
    max_matrix = agg_p_nom_minmax["max"].to_xarray().unstack().reindex_like(lhs)

    n.model.add_constraints(
        lhs >= min_matrix, name="agg_p_nom_min", mask=min_matrix.notnull()
    )
    n.model.add_constraints(
        lhs <= max_matrix, name="agg_p_nom_max", mask=max_matrix.notnull()
    )


def add_EQ_constraints(n, o, scaling=1e-1):
    float_regex = "[0-9]*\.?[0-9]+"
    level = float(re.findall(float_regex, o)[0])
    if o[-1] == "c":
        ggrouper = n.generators.bus.map(n.buses.country)
        lgrouper = n.loads.bus.map(n.buses.country)
        sgrouper = n.storage_units.bus.map(n.buses.country)
    else:
        ggrouper = n.generators.bus
        lgrouper = n.loads.bus
        sgrouper = n.storage_units.bus
    load = (
        n.snapshot_weightings.generators
        @ n.loads_t.p_set.groupby(lgrouper, axis=1).sum()
    )
    inflow = (
        n.snapshot_weightings.stores
        @ n.storage_units_t.inflow.groupby(sgrouper, axis=1).sum()
    )
    inflow = inflow.reindex(load.index).fillna(0.0)
    rhs = scaling * (level * load - inflow)
    dispatch_variable = n.model["Generator-p"].T
    lhs_gen = (
        (dispatch_variable * (n.snapshot_weightings.generators * scaling))
        .groupby_sum(ggrouper)
        .sum("snapshot")
    )
    if not n.storage_units_t.inflow.empty:
        spillage_variable = n.model["StorageUnit-spill"]
        lhs_spill = (
            (spillage_variable * (-n.snapshot_weightings.stores * scaling))
            .groupby_sum(sgrouper)
            .sum("snapshot")
        )
        lhs = merge(lhs_gen, lhs_spill)
    else:
        lhs = lhs_gen
    n.model.add_constraints(lhs >= rhs, name="equity_min")


def add_BAU_constraints(n, config):
    mincaps = pd.Series(config["electricity"]["BAU_mincapacities"])
    # lhs = (
    #     linexpr((1, get_var(n, "Generator", "p_nom")))
    #     .groupby(n.generators.carrier)
    #     .apply(join_exprs)
    # )
    # define_constraints(n, lhs, ">=", mincaps[lhs.index], "Carrier", "bau_mincaps")
    capacity_variable = n.model["Generator-p_nom"]
    ext_i = n.generators.query("p_nom_extendable")
    ext_carrier_i = ext_i.carrier.rename_axis("Generator-ext")
    lhs = capacity_variable.groupby_sum(ext_carrier_i)
    rhs = mincaps[lhs.coords["carrier"].values].rename_axis("carrier")
    n.model.add_constraints(lhs >= rhs, name="bau_mincaps")

    maxcaps = pd.Series(
        config["electricity"].get("BAU_maxcapacities", {key: np.inf for key in ext_c})
    )
    lhs = (
        linexpr((1, get_var(n, "Generator", "p_nom")))
        .groupby(n.generators.carrier)
        .apply(join_exprs)
    )
    define_constraints(n, lhs, "<=", maxcaps[lhs.index], "Carrier", "bau_maxcaps")


def add_SAFE_constraints(n, config):
    peakdemand = n.loads_t.p_set.sum(axis=1).max()
    margin = 1.0 + config["electricity"]["SAFE_reservemargin"]
    reserve_margin = peakdemand * margin
    conv_techs = config["plotting"]["conv_techs"]
    ext_gens_i = n.generators.query("carrier in @conv_techs & p_nom_extendable").index
    capacity_variable = n.model["Generator-p_nom"]
    ext_cap_var = capacity_variable.sel({"Generator-ext": ext_gens_i})
    lhs = ext_cap_var.sum()
    exist_conv_caps = n.generators.query(
        "~p_nom_extendable & carrier in @conv_techs"
    ).p_nom.sum()
    rhs = reserve_margin - exist_conv_caps
    n.model.add_constraints(lhs >= rhs, name="safe_mintotalcap")


def add_operational_reserve_margin_constraint(n, sns, config):
    reserve_config = config["electricity"]["operational_reserve"]
    EPSILON_LOAD = reserve_config["epsilon_load"]
    EPSILON_VRES = reserve_config["epsilon_vres"]
    CONTINGENCY = reserve_config["contingency"]

    # Reserve Variables
    n.model.add_variables(
        0, np.inf, coords=[sns, n.generators.index], name="Generator-r"
    )
    reserve = n.model["Generator-r"]
    lhs = reserve.sum("Generator")

    # Share of extendable renewable capacities
    ext_i = n.generators.query("p_nom_extendable").index
    vres_i = n.generators_t.p_max_pu.columns
    if not ext_i.empty and not vres_i.empty:
        capacity_factor = n.generators_t.p_max_pu[vres_i.intersection(ext_i)]
        renewable_capacity_variables = (
            n.model["Generator-p_nom"]
            .sel({"Generator-ext": vres_i.intersection(ext_i)})
            .rename({"Generator-ext": "Generator"})
        )
        lhs = merge(
            lhs,
            (renewable_capacity_variables * (-EPSILON_VRES * capacity_factor)).sum(
                ["Generator"]
            ),
        )

    # Total demand per t
    demand = n.loads_t.p_set.sum(1)

    # VRES potential of non extendable generators
    capacity_factor = n.generators_t.p_max_pu[vres_i.difference(ext_i)]
    renewable_capacity = n.generators.p_nom[vres_i.difference(ext_i)]
    potential = (capacity_factor * renewable_capacity).sum(1)

    # Right-hand-side
    rhs = EPSILON_LOAD * demand + EPSILON_VRES * potential + CONTINGENCY

    # define_constraints(n, lhs, ">=", rhs, "Reserve margin")
    n.model.add_constraints(lhs >= rhs, name="reserve_margin")


def update_capacity_constraint(n):
    gen_i = n.generators.index
    ext_i = n.generators.query("p_nom_extendable").index
    fix_i = n.generators.query("not p_nom_extendable").index

    dispatch = n.model["Generator-p"]
    reserve = n.model["Generator-r"]
    p_max_pu = get_as_dense(n, "Generator", "p_max_pu")
    capacity_fixed = n.generators.p_nom[fix_i]

    lhs = merge(
        dispatch * 1,
        reserve * 1,
    )

    if not ext_i.empty:
        capacity_variable = n.model["Generator-p_nom"]
        lhs = merge(
            lhs,
            capacity_variable.rename({"Generator-ext": "Generator"}) * -p_max_pu[ext_i],
        )

    rhs = (p_max_pu[fix_i] * capacity_fixed).reindex(columns=gen_i)
    n.model.add_constraints(
        lhs <= rhs, name="gen_updated_capacity_constraint", mask=rhs.notnull()
    )


def add_operational_reserve_margin(n, sns, config):
    """
    Build reserve margin constraints based on the formulation given in
    https://genxproject.github.io/GenX/dev/core/#Reserves.
    """

    # define_variables(n, 0, np.inf, "Generator", "r", axes=[sns, n.generators.index])
    # add_operational_reserve_margin_constraint(n, config)
    add_operational_reserve_margin_constraint(n, sns, config)
    update_capacity_constraint(n)


def add_battery_constraints(n):
    nodes = n.buses.index[n.buses.carrier == "battery"]
    # if nodes.empty or ("Link", "p_nom") not in n.variables.index:
    if nodes.empty:
        return
    vars_link = n.model["Link-p_nom"]
    eff = n.links.loc[nodes + " discharger", "efficiency"]
    lhs = merge(
        vars_link.sel({"Link-ext": nodes + " charger"}) * 1,
        # for some reasons, eff is one element longer as compared with vars_link
        vars_link.sel({"Link-ext": nodes + " discharger"}) * -eff[0],
    )
    n.model.add_constraints(lhs == 0, name="link_charger_ratio")


def add_RES_constraints(n, res_share):
    lgrouper = n.loads.bus.map(n.buses.country)
    ggrouper = n.generators.bus.map(n.buses.country)
    sgrouper = n.storage_units.bus.map(n.buses.country)
    cgrouper = n.links.bus0.map(n.buses.country)

    logger.warning(
        "The add_RES_constraints functionality is still work in progress. "
        "Unexpected results might be incurred, particularly if "
        "temporal clustering is applied or if an unexpected change of technologies "
        "is subject to the obtimisation."
    )

    load = (
        n.snapshot_weightings.generators
        @ n.loads_t.p_set.groupby(lgrouper, axis=1).sum()
    )

    rhs = res_share * load

    res_techs = [
        "solar",
        "onwind",
        "offwind-dc",
        "offwind-ac",
        "battery",
        "hydro",
        "ror",
    ]
    charger = ["H2 electrolysis", "battery charger"]
    discharger = ["H2 fuel cell", "battery discharger"]

    gens_i = n.generators.query("carrier in @res_techs").index
    stores_i = n.storage_units.query("carrier in @res_techs").index
    charger_i = n.links.query("carrier in @charger").index
    discharger_i = n.links.query("carrier in @discharger").index

    # Generators
    lhs_gen = (
        linexpr(
            (n.snapshot_weightings.generators, get_var(n, "Generator", "p")[gens_i].T)
        )
        .T.groupby(ggrouper, axis=1)
        .apply(join_exprs)
    )

    # StorageUnits
    lhs_dispatch = (
        (
            linexpr(
                (
                    n.snapshot_weightings.stores,
                    get_var(n, "StorageUnit", "p_dispatch")[stores_i].T,
                )
            )
            .T.groupby(sgrouper, axis=1)
            .apply(join_exprs)
        )
        .reindex(lhs_gen.index)
        .fillna("")
    )

    lhs_store = (
        (
            linexpr(
                (
                    -n.snapshot_weightings.stores,
                    get_var(n, "StorageUnit", "p_store")[stores_i].T,
                )
            )
            .T.groupby(sgrouper, axis=1)
            .apply(join_exprs)
        )
        .reindex(lhs_gen.index)
        .fillna("")
    )

    # Stores (or their resp. Link components)
    # Note that the variables "p0" and "p1" currently do not exist.
    # Thus, p0 and p1 must be derived from "p" (which exists), taking into account the link efficiency.
    lhs_charge = (
        (
            linexpr(
                (
                    -n.snapshot_weightings.stores,
                    get_var(n, "Link", "p")[charger_i].T,
                )
            )
            .T.groupby(cgrouper, axis=1)
            .apply(join_exprs)
        )
        .reindex(lhs_gen.index)
        .fillna("")
    )

    lhs_discharge = (
        (
            linexpr(
                (
                    n.snapshot_weightings.stores.apply(
                        lambda r: r * n.links.loc[discharger_i].efficiency
                    ),
                    get_var(n, "Link", "p")[discharger_i],
                )
            )
            .groupby(cgrouper, axis=1)
            .apply(join_exprs)
        )
        .reindex(lhs_gen.index)
        .fillna("")
    )

    # signs of resp. terms are coded in the linexpr.
    # todo: for links (lhs_charge and lhs_discharge), account for snapshot weightings
    lhs = lhs_gen + lhs_dispatch + lhs_store + lhs_charge + lhs_discharge

    define_constraints(n, lhs, "=", rhs, "RES share")


def extra_functionality(n, snapshots):
    """
    Collects supplementary constraints which will be passed to
    ``pypsa.linopf.network_lopf``.

    If you want to enforce additional custom constraints, this is a good location to add them.
    The arguments ``opts`` and ``snakemake.config`` are expected to be attached to the network.
    """
    opts = ""
    config = n.config
    if "BAU" in opts and n.generators.p_nom_extendable.any():
        add_BAU_constraints(n, config)
    if "SAFE" in opts and n.generators.p_nom_extendable.any():
        add_SAFE_constraints(n, config)
    if "CCL" in opts and n.generators.p_nom_extendable.any():
        add_CCL_constraints(n, config)
    reserve = config["electricity"].get("operational_reserve", {})
    if reserve.get("activate"):
        add_operational_reserve_margin(n, snapshots, config)
    # for o in opts:
    #     if "RES" in o:
    #         res_share = float(re.findall("[0-9]*\.?[0-9]+$", o)[0])
    #         add_RES_constraints(n, res_share)
    # for o in opts:
    #     if "EQ" in o:
    #         add_EQ_constraints(n, o)
    add_battery_constraints(n)


def solve_network(n, config, solving, **kwargs):
    set_of_options = solving["solver"]["options"]
    cf_solving = solving["options"]

    kwargs["solver_options"] = (
        solving["solver_options"][set_of_options] if set_of_options else {}
    )
    kwargs["solver_name"] = solving["solver"]["name"]
    kwargs["extra_functionality"] = extra_functionality
    kwargs["transmission_losses"] = cf_solving.get("transmission_losses", False)
    kwargs["linearized_unit_commitment"] = cf_solving.get(
        "linearized_unit_commitment", False
    )
    kwargs["assign_all_duals"] = cf_solving.get("assign_all_duals", False)
    kwargs["io_api"] = cf_solving.get("io_api", None)

    # add to network for extra_functionality
    n.config = config

    skip_iterations = cf_solving.get("skip_iterations", False)
    if not n.lines.s_nom_extendable.any():
        skip_iterations = True
        logger.info("No expandable lines found. Skipping iterative solving.")

    # status, condition = run_glpk(**kwargs)

    status, condition = n.optimize.optimize_transmission_expansion_iteratively(**kwargs)

    if skip_iterations:
        status, condition = n.optimize(**kwargs)
    else:
        kwargs["track_iterations"] = (cf_solving.get("track_iterations", False),)
        kwargs["min_iterations"] = (cf_solving.get("min_iterations", 4),)
        kwargs["max_iterations"] = (cf_solving.get("max_iterations", 6),)
        status, condition = n.optimize.optimize_transmission_expansion_iteratively(
            **kwargs
        )

    if status != "ok":  # and not rolling_horizon:
        logger.warning(
            f"Solving status '{status}' with termination condition '{condition}'"
        )
    if "infeasible" in condition:
        labels = n.model.compute_infeasibilities()
        logger.info(f"Labels:\n{labels}")
        n.model.print_infeasibilities()
        raise RuntimeError("Solving status 'infeasible'")

    return n


if __name__ == "__main__":
    if "snakemake" not in globals():
        from _helpers import mock_snakemake

        os.chdir(os.path.dirname(os.path.abspath(__file__)))
        snakemake = mock_snakemake(
            "solve_network",
            simpl="",
            clusters="54",
            ll="copt",
            opts="Co2L-1H",
        )
    configure_logging(snakemake)

    # tmpdir = snakemake.params.solving.get("tmpdir")
    # if tmpdir is not None:
    #     Path(tmpdir).mkdir(parents=True, exist_ok=True)
    opts = snakemake.wildcards.opts.split("-")
    solve_opts = snakemake.params.solving["options"]

    n = pypsa.Network(snakemake.input[0])
    # TODO Double-check handling the augmented case
    # if snakemake.params.augmented_line_connection.get("add_to_snakefile"):
    #    n.lines.loc[
    #        n.lines.index.str.contains("new"), "s_nom_min"
    #    ] = snakemake.params.augmented_line_connection.get("min_expansion")
    n = prepare_network(n, solve_opts, config=solve_opts)
    n = solve_network(
        n,
        config=snakemake.config,
        solving=snakemake.params.solving,
        solver_logfile=snakemake.log.solver,
    )
    n.meta = dict(snakemake.config, **dict(wildcards=dict(snakemake.wildcards)))
    n.export_to_netcdf(snakemake.output[0])
