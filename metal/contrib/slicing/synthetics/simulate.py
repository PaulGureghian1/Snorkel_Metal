"""Runs simulations over equal weights, manual reweighting,
and attention based models.

Sample command:
python simulate.py --var cov --save-dir results/test --n 50 --x-range 0.6 0.7 0.8 0.9 1.0
"""

import argparse
import datetime
import json
import os
import sys
from collections import defaultdict

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from synthetics_utils import (
    generate_label_matrix,
    generate_multi_mode_data,
    plot_slice_scores,
)
from tqdm import tqdm

from metal.contrib.slicing.online_dp import (
    LinearModule,
    MLPModule,
    SliceDPModel,
)

sys.path.append("/dfs/scratch0/vschen/metal")


def train_models(X, L, accs, verbose=False):
    """
    Trains baseline, oracle, and attention model
    Args:
        - X: features
        - L: LF matrix
        - accs: [list of floats] accuracies for LFs
    Returns:
        - model_[0,1,2]: trained baseline, oracle, and attention model
    """

    m = np.shape(L)[1]  # num LFs
    d = X.shape[1]  # num features
    X_train = torch.from_numpy(X.astype(np.float32))
    L_train = torch.from_numpy(L.astype(np.float32))

    train_kwargs = {
        "batch_size": 1000,
        "n_epochs": 250,
        "print_every": 50,
        "validation_metric": "f1",
        "disable_prog_bar": True,
    }

    # baseline model, no attention
    r = 2
    baseline_model = SliceDPModel(
        LinearModule(d, r, bias=True),
        accs,
        r=r,
        rw=False,
        verbose=verbose,
        use_cuda=True,
    )
    baseline_model.train_model((X_train, L_train), **train_kwargs)

    # oracle, manual reweighting
    # currently hardcode weights so LF[-1] has double the weight
    weights = np.ones(m, dtype=np.float32)
    weights[-1] = 2.0
    r = 2
    manual_model = SliceDPModel(
        LinearModule(d, r, bias=True),
        accs,
        r=r,
        rw=False,
        L_weights=weights,
        verbose=verbose,
        use_cuda=True,
    )
    manual_model.train_model((X_train, L_train), **train_kwargs)

    # our model, with attention
    r = 2
    attention_model = SliceDPModel(
        LinearModule(d, r, bias=True),
        accs,
        r=r,
        rw=True,
        verbose=verbose,
        use_cuda=True,
    )
    attention_model.train_model((X_train, L_train), **train_kwargs)

    return baseline_model, manual_model, attention_model


def eval_model(model, data, eval_dict):
    """Evaluates models according to indexes in 'eval_dict'
    Args:
        model: trained model to evaluate
        data: (X,Y) full test set to evaluate on
        eval_dict: mapping eval slice {"slice_name":idx}
            where idx is list of indexes for corresponding slice
    Returns:
        results_dict: mapping {"slice_name": scores}
            includes "overall" accuracy by default
    """
    X, Y = data
    # conver to multiclass labels
    if -1 in Y:
        Y[Y == -1] = 2

    data = (
        torch.from_numpy(X.astype(np.float32)),
        torch.from_numpy(Y.astype(np.float32)),
    )

    slice_scores = {}
    for slice_name, eval_idx in eval_dict.items():
        slice_scores[slice_name] = model.score_on_slice(
            data, eval_idx, metric="accuracy", verbose=False
        )

    slice_scores["overall"] = model.score(
        data, metric="accuracy", verbose=False
    )
    return slice_scores


def simulate(config):
    """Simulates models comparing baseline, manual, and attention models
    over the specified config.
    """

    # to collect scores for all models
    baseline_scores, manual_scores, attention_scores = (
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
    )

    # get config variables
    N = config["N"]
    mus = config["mus"]
    labels = config["labels"]
    props = config["props"]
    accs = config["accs"]
    covs = config["covs"]
    op = config["op"]
    # m = np.shape(config["accs"])[0]
    num_trials = config["num_trials"]
    x_range = config["x_range"]
    var_name = config["x_var"]

    # for each value, run num_trials simulations
    for x in x_range:
        print(f"Simulating: {var_name}={x}")
        for _ in tqdm(range(num_trials)):

            # overwrite default config with simulation var
            if var_name == "op":
                op = x
            elif var_name == "acc":
                accs[-1] = x
            elif var_name == "cov":
                covs[-1] = x

            # generate data
            X, Y, C = generate_multi_mode_data(N, mus, props, labels)
            L, overlap_idx = generate_label_matrix(
                N, accs, covs, Y, C, overlap_portion=op
            )

            # train the models
            baseline_model, manual_model, attention_model = train_models(
                X, L, accs
            )

            # score the models
            S0_idx, S1_idx, S2_idx = (
                np.where(C == 0)[0],
                np.where(C == 1)[0],
                np.where(C == 2)[0],
            )
            eval_dict = {
                "overlap": overlap_idx,
                "S0": S0_idx,
                "S1": S1_idx,
                "S2": S2_idx,
            }
            baseline_scores[x].append(
                eval_model(baseline_model, (X, Y), eval_dict)
            )
            manual_scores[x].append(eval_model(manual_model, (X, Y), eval_dict))
            attention_scores[x].append(
                eval_model(attention_model, (X, Y), eval_dict)
            )

    return baseline_scores, manual_scores, attention_scores


if __name__ == "__main__":
    import warnings

    warnings.filterwarnings("ignore")
    # TODO: fix warnings

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--variable",
        choices=["op", "acc", "cov"],
        help="variable we are varying in simulation",
    )
    parser.add_argument("--save-dir", type=str, help="where to save results")
    parser.add_argument(
        "--n", type=int, default=25, help="num trials to run of simulation"
    )
    parser.add_argument(
        "--x-range",
        type=float,
        nargs="+",
        default=None,
        help="range of values to scan over",
    )
    args = parser.parse_args()

    # define simulation config
    config = {
        "N": 10000,  # num data points
        "mus": [  # centers of modes
            np.array([-3, 0]),  # Mode 1: Y = -1
            np.array([3, 0]),  # Mode 2: Y = 1
            np.array([6, -3]),  # Mode 3: Y = -1
        ],
        "labels": [-1, 1, -1],  # labels of each slice
        "props": [0.25, 0.5, 0.25],  # porportion of data in each slice
        "accs": np.array([0.75, 0.75, 0.75]),  # accuracy of LFs
        "covs": np.array([0.9, 0.9, 0.9]),  # coverage of LFs
        "op": 0.05,  # overlap proportion btwn head and torso
        "num_trials": args.n,  # num trials of simulation to run
        "x_range": np.linspace(0, 1.0, 5)
        if args.x_range is None
        else list(args.x_range),
        "x_var": args.variable,
    }

    # run simulations
    baseline_scores, manual_scores, attention_scores = simulate(config)

    # save scores and plot
    results = {
        "baseline": dict(baseline_scores),
        "manual": dict(manual_scores),
        "attention": dict(attention_scores),
    }
    print(f"Saving to {args.save_dir}")
    results_path = os.path.join(args.save_dir, f"{args.variable}-results.json")
    os.makedirs(args.save_dir, exist_ok=True)
    json.dump(results, open(results_path, "w"))
    if args.variable == "op":
        xlabel = "Overlap Proportion"
    elif args.variable == "acc":
        xlabel = "Head Accuracy"
    elif args.variable == "cov":
        xlabel = "Head Coverage"
    plot_slice_scores(results, "S2", xlabel=xlabel, save_dir=args.save_dir)
    plot_slice_scores(results, "S1", xlabel=xlabel, save_dir=args.save_dir)
    plot_slice_scores(results, "S0", xlabel=xlabel, save_dir=args.save_dir)
    plot_slice_scores(results, "overall", xlabel=xlabel, save_dir=args.save_dir)
    plot_slice_scores(results, "overlap", xlabel=xlabel, save_dir=args.save_dir)
