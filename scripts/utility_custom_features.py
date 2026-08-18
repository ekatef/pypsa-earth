import logging

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from pyproj import Transformer
from pypsa.io import import_components_from_dataframe, import_series_from_dataframe
from rasterio.features import rasterize
from rasterio.mask import mask
from rasterio.transform import from_bounds
from shapely import wkt
from shapely.ops import transform as shapely_transform

logger = logging.getLogger(__name__)


def annual_gwh_to_average_mw(energy_gwh, hours_per_year=8760):
    """Convert annual energy in GWh to average power in MW."""
    return energy_gwh * 1000 / hours_per_year


def load_interconnector_data(countries_path, links_path, substations_path, year=None):
    """Load interconnector input data from CSV files.

    If sapp_countries.csv contains a 'year' column, the row set matching
    *year* is selected.  When *year* is None or not present in the data the
    most recent available year is used as a fallback.
    """
    countries = pd.read_csv(countries_path)

    if "year" in countries.columns:
        available_years = countries["year"].unique()
        if year is None or year not in available_years:
            if year is not None:
                logger.warning(
                    f"No trade data for year {year} in {countries_path}; "
                    f"falling back to {available_years.max()}."
                )
            year = available_years.max()
        countries = countries[countries["year"] == year].drop(columns=["year"])

    return (
        countries,
        pd.read_csv(links_path),
        pd.read_csv(substations_path),
    )


def find_nearest_bus(n, lat, lon, distance_crs="EPSG:20935", country="PT"):
    """Return the nearest Zambian bus to a given latitude and longitude."""
    buses = n.buses[n.buses["country"] == country].copy()
    buses = gpd.GeoDataFrame(
        buses,
        geometry=gpd.points_from_xy(buses["x"], buses["y"]),
        crs="EPSG:4326",
    ).to_crs(distance_crs)
    target_point = (
        gpd.GeoSeries.from_xy([lon], [lat], crs="EPSG:4326")
        .to_crs(distance_crs)
        .iloc[0]
    )
    distances = buses.geometry.distance(target_point)
    return distances.idxmin()


def add_foreign_buses(n, power_pool_countries):
    """Add neighbouring-country buses to the network."""
    for _, row in power_pool_countries.iterrows():
        country = row["country"]
        if country not in n.buses.index:
            n.add("Bus", country, x=row["lon"], y=row["lat"], carrier="AC")
            n.buses.loc[country, "country"] = country
    return n


def add_cross_border_links(
    n, power_pool_links, substation_dict, distance_crs, country="PT"
):
    """Add cross-border links to the network."""
    for _, row in power_pool_links.iterrows():
        name = row["name"]
        if row["from_country"] == country:
            lat, lon = substation_dict[name]
            bus0 = find_nearest_bus(n, lat, lon, distance_crs)
        else:
            bus0 = row["from_country"]
        if row["to_country"] == country:
            lat, lon = substation_dict[name]
            bus1 = find_nearest_bus(n, lat, lon, distance_crs)
        else:
            bus1 = row["to_country"]

        if name not in n.links.index:
            n.add(
                "Link",
                name,
                bus0=bus0,
                bus1=bus1,
                carrier="AC",
                p_nom=row["capacity_mw"],
                efficiency=1.0,
                p_min_pu=-1.0,
            )
    return n


def add_trade_components(n, power_pool_countries, hours_per_year=8760):
    """Add import loads and export generators for neighbouring countries."""
    for _, row in power_pool_countries.iterrows():
        country = row["country"]
        if country not in n.buses.index:
            continue

        load_name = f"import_{country}"
        gen_name = f"export_{country}"

        if load_name not in n.loads.index:
            n.add(
                "Load",
                load_name,
                bus=country,
                carrier="import",
                p_set=annual_gwh_to_average_mw(row["demand_gwh"], hours_per_year),
            )

        if gen_name not in n.generators.index:
            n.add(
                "Generator",
                gen_name,
                bus=country,
                carrier="export",
                p_nom=annual_gwh_to_average_mw(row["generation_gwh"], hours_per_year),
                marginal_cost=row["marginal_cost"],
            )
    return n


def add_interconnectors(
    n,
    power_pool_countries,
    power_pool_links,
    substations,
    distance_crs,
    hours_per_year=8760,
):
    """Add foreign buses, interconnectors, and trade components to the network."""
    substation_dict = {
        row["name"]: (row["lat"], row["lon"]) for _, row in substations.iterrows()
    }

    n = add_foreign_buses(n, power_pool_countries)
    n = add_cross_border_links(n, power_pool_links, substation_dict, distance_crs)
    n = add_trade_components(n, power_pool_countries, hours_per_year)

    return n
