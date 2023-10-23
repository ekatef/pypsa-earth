# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText:  PyPSA-Earth and PyPSA-Eur Authors
#
# SPDX-License-Identifier: CC0-1.0
"""
The script compares two yamls and outputs as csv-s a difference in columns (if
any) and a difference for the common columns. The nested yaml structure is
nested with `+` used to concatenate names of the parameters on different
levels/

Inputs
------

- first_config a path to the first yaml
- second_config a path to the first yaml

Outputs
-------

- ``col_diff.csv`` to show a difference in the columns
- ``config_diff.csv`` to show a difference for common columns
"""

import json

import pandas as pd
import yaml

# first_config = "./results/test-sector-overnight-flexible/config.yaml"
# second_config = "./results/test-sector-overnight-rigid/config.yaml"

first_config = "config.default.yaml"
second_config = "config.tutorial.yaml"


with open(first_config, "r") as yaml_in, open("first.json", "w") as json_out:
    yaml_object = yaml.safe_load(yaml_in)  # yaml_object will be a list or a dict
    json_text = json.dump(yaml_object, json_out)
with open(second_config, "r") as yaml_in, open("second.json", "w") as json_out:
    yaml_object = yaml.safe_load(yaml_in)  # yaml_object will be a list or a dict
    json_text = json.dump(yaml_object, json_out)

# TODO There should be way to transform yaml -> json without json output to a disk
with open("first.json") as f:
    flex_json = json.load(f)
with open("second.json") as f:
    rigid_json = json.load(f)

df_second_config = pd.json_normalize(rigid_json, sep="+")
df_first_config = pd.json_normalize(flex_json, sep="+")

col_diff = set(df_second_config.columns).symmetric_difference(df_first_config.columns)

if len(col_diff) > 0:
    pd.DataFrame(col_diff).to_csv("col_diff.csv")
    print("That is the difference in column names:")
    print(col_diff)
    print("\n\r")

    df_first_config2 = df_second_config.drop(
        set(df_second_config.columns).difference(df_first_config.columns), axis=1
    ).reindex(sorted(df_second_config.columns), axis=1)
    df_second_config2 = df_second_config.drop(
        set(df_second_config.columns).difference(df_first_config.columns), axis=1
    ).reindex(sorted(df_second_config.columns), axis=1)
    df_compare = df_second_config2.compare(df_first_config2)
else:
    df_compare = df_second_config.compare(df_first_config)

print("That is the comparison result:")
print(df_compare)
df_compare.to_csv("config_diff.csv")
