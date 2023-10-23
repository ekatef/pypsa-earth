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

- flex_config a path to the first yaml
- rigid_config a path to the first yaml

Outputs
-------

- ``col_diff.csv`` to show a difference in the columns
- ``config_diff.csv`` to show a difference for common columns
"""

import json

import pandas as pd
import yaml

flex_config = "./results/test-sector-overnight-flexible/config.yaml"
rigid_config = "./results/test-sector-overnight-rigid/config.yaml"


with open(flex_config, "r") as yaml_in, open("flex.json", "w") as json_out:
    yaml_object = yaml.safe_load(yaml_in)  # yaml_object will be a list or a dict
    json_text = json.dump(yaml_object, json_out)
with open(rigid_config, "r") as yaml_in, open("rigid.json", "w") as json_out:
    yaml_object = yaml.safe_load(yaml_in)  # yaml_object will be a list or a dict
    json_text = json.dump(yaml_object, json_out)

# TODO There should be way to transform yaml -> json without json output to a disk
with open("flex.json") as f:
    flex_json = json.load(f)
with open("rigid.json") as f:
    rigid_json = json.load(f)

df_rigid_config = pd.json_normalize(rigid_json, sep="+")
df_flex_config = pd.json_normalize(flex_json, sep="+")

col_diff = set(df_rigid_config.columns).symmetric_difference(df_flex_config.columns)

if len(col_diff) > 0:
    pd.DataFrame(col_diff).to_csv("col_diff.csv")
    print("That is the difference in column names:")
    print(col_diff)
    print("\n\r")

    df_rigid_config2 = df_rigid_config.drop(
        set(df_rigid_config.columns).difference(df_flex_config.columns), axis=1
    )
    df_flex_config2 = df_flex_config.drop(
        set(df_flex_config.columns).difference(df_flex_config.columns), axis=1
    )
    df_compare = df_rigid_config2.compare(df_flex_config2)
else:
    df_compare = df_rigid_config.compare(df_flex_config)

print("That is the comparison result:")
print(df_compare)
df_compare.to_csv("config_diff.csv")
