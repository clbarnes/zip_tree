#!/usr/bin/env python
import math
from pathlib import Path
import logging
import subprocess as sp
import os
from contextlib import contextmanager
import sys
from argparse import ArgumentParser
from typing import Optional, Callable, Hashable, Tuple
import re

from tqdm import tqdm
import networkx as nx

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

DEFAULT_MAX_ZIP_SIZE = 100 * 1024 ** 3  # 100GiB
DEFAULT_LIMIT_PER_TIB = 100_000

SEP = "\t"

ROOT = Path(".")
LENGTH = 72629

TOTAL = 0


def ieee_size(b, decimals=2):
    k = 1024
    unit = ""
    units = ["Ki", "Mi", "Gi", "Ti", "Pi"]
    while b > k:
        unit = units.pop(0)
        b /= k
    return f"{b:.{decimals}f}{unit}B"


def count_lines(path):
    logger.info("Counting lines in %s", path)
    result = sp.run(["wc", "-l", os.fspath(path)], capture_output=True, check=True)
    return int(result.stdout.split()[0])


def is_path(obj):
    return isinstance(obj, os.PathLike) or (isinstance(obj, str) and obj != "-")


def sizes_to_graph(fpath, total=True, progress=True):
    if is_path(fpath) and total is True:
        total = count_lines(fpath)

    logger.info("constructing tree")
    g = nx.OrderedDiGraph()
    total_size = 0
    total_descendants = 0

    with ensure_file(fpath, "r") as f:
        for line in tqdm(
            f, total=total, desc="adding files to tree", disable=not progress
        ):
            *path_str_items, size_str = line.strip().split(SEP)
            path_str = "\t".join(path_str_items)
            size = int(size_str)
            path = Path(path_str)
            g.add_node(path, size=size, descendants=0)
            total_size += size
            total_descendants += 1

            child = path
            for parent in path.parents:
                parent_in_g = parent in g

                # calculate size and descendants up front: probably slower
                # child_d = g.nodes[child]
                # if parent_in_g:
                #     parent_d = g.nodes[parent]
                #     parent_d["size"] += child_d["size"]
                #     parent_d["descendants"] += 1 + child_d["descendants"]
                # else:
                #     total_descendants += 1
                #     g.add_node(
                #         parent,
                #         size=child_d["size"],
                #         descendants=child_d["descendants"] + 1
                #     )

                g.add_edge(parent, child)
                # allow size and descendants to be calculated later
                if parent_in_g:
                    break
                total_descendants += 1
                child = parent

    logger.info(
        "constructed tree with files of total size %s",
        ieee_size(total_size),
    )
    g.graph["total_size"] = total_size
    g.graph["total_descendants"] = total_descendants - 1
    return g


def size_descendants(g: nx.DiGraph, node):
    data = g.nodes[node]
    s = data.get("size")
    d = data.get("descendants")
    if d is None or not s:
        s = s or 0
        d = d or 0
        for child in g.successors(node):
            c_s, c_d = size_descendants(g, child)
            s += c_s
            d += 1 + c_d
        data["size"] = s
        data["descendants"] = d
    return s, d


def node_size(g: nx.DiGraph, node):
    return size_descendants(g, node)[0]


def node_descendants(g: nx.DiGraph, node):
    return size_descendants(g, node)[1]


@contextmanager
def ensure_file(obj, mode="r"):
    mode = mode or "r"
    should_write = mode[0] in "wa" or "+" in mode
    should_read = mode[0] == "r"

    if not obj or obj == "-":
        if should_read:
            if should_write:
                raise ValueError("stdin/stdout cannot be both written and read")
            yield sys.stdin
        elif should_write:
            yield sys.stdout
        else:
            raise ValueError("unknown mode: " + mode)

    else:
        if isinstance(obj, (str, os.PathLike)):
            with open(obj, mode) as f:
                yield f
        else:
            if (
                should_read
                and not hasattr(obj, "read")
                or should_write
                and not hasattr(obj, "write")
            ):
                raise ValueError("Object could not be interpreted as file-like or path")
            else:
                yield obj


def interpret_bytes(s):
    result = re.fullmatch(
        r"\s*(?P<n>\d*\.?\d*)\s*(?P<prefix>[kKMGTPEZY]?i?)(?P<unit>[bB]?)\s*", s
    )
    if not result:
        raise ValueError("Could not interpret size from string: " + s)
    groups = result.groupdict()
    num = float(groups["n"] or 0)
    mult_by = 1
    if prefix := groups["prefix"]:
        powers = {
            "": 0,
            "k": 1,
            "K": 1,
            "M": 2,
            "G": 3,
            "T": 4,
            "P": 5,
            "E": 6,
            "Z": 7,
            "Y": 8,
        }
        if prefix.endswith("i"):
            mult_by *= 1024 ** powers[prefix[:-1]]
        else:
            mult_by *= 1000 ** powers[prefix]
    if b := groups["unit"]:
        if b == "b":
            mult_by /= 8
    return int(num * mult_by)


def dfs(
    g: nx.DiGraph,
    node: Optional[Hashable] = None,
    yield_abort_if: Optional[
        Callable[[nx.DiGraph, Hashable], Tuple[bool, bool]]
    ] = None,
    progress=True,
):
    if node is None:
        node = ROOT
    if yield_abort_if is None:
        yield_abort_if = lambda _x, _y: (True, False)  # noqa

    with tqdm(
        desc="calculating zip dirs",
        total=node_descendants(g, node) + 1,
        disable=not progress,
    ) as pbar:
        to_visit = [node]
        while to_visit:
            node = to_visit.pop()
            should_yield, should_abort = yield_abort_if(g, node)
            if should_yield:
                logger.debug("Yielding %s", node)
                yield node
            if should_abort:
                logger.debug("Skipping subtree below %s", node)
                pbar.update(node_descendants(g, node) + 1)
            else:
                to_visit.extend(sorted(g.successors(node), reverse=True))
                pbar.update(1)


def yield_zips(
    g: nx.DiGraph,
    max_zip_bytes=100 * 1024 ** 3,  # 100GB
    max_files_per_TiB=100_000,
    progress=True,
):
    def yield_abort_if(graph, node):
        size, desc = size_descendants(graph, node)
        to_yield = size < max_zip_bytes
        to_abort = to_yield or (desc / (size / 1024 ** 4)) < max_files_per_TiB
        return to_yield, to_abort

    archived_inode_count = 0
    archive_count = 0
    archived_size = 0
    for node in dfs(g, ROOT, yield_abort_if, progress):
        archive_count += 1
        d = g.nodes[node]
        archived_inode_count += d["descendants"]
        archived_size += d["size"]
        yield node

    logger.info(
        "%s file(s) will be zipped into %s archive(s)",
        archived_inode_count,
        archive_count,
    )
    final_per_TiB = (
        g.graph["total_descendants"] - archived_inode_count + archive_count
    ) / g.graph["total_size"]
    logger.info(
        "Assuming zero compression, total set comprises <=%s inode(s) per TiB",
        math.ceil(final_per_TiB),
    )


def make_parser():
    parser = ArgumentParser()
    parser.add_argument(
        "input",
        nargs="?",
        help="input file (empty or - to use stdin) "
        "with lines of format `{filename}\\t{n_bytes}`",
    )
    parser.add_argument(
        "output", nargs="?", help="output file (empty or - to use stdout)"
    )
    parser.add_argument(
        "-t",
        "--total",
        nargs="?",
        const=True,
        help=(
            "Total number of lines expected to add to graph, "
            "for progress-reporting purposes. "
            "If `input` is a path AND `total` is given without a value, "
            "the input file's lines are counted with `wc -l` "
            "before processing."
        ),
    )
    parser.add_argument(
        "-z",
        "--max-zip-size",
        type=interpret_bytes,
        default=DEFAULT_MAX_ZIP_SIZE,
        help="Only directories whose contents are smaller than this will be yielded, "
        f"default {ieee_size(DEFAULT_MAX_ZIP_SIZE)}. "
        "Understands SI and IEEE prefixes; bits and Bytes (default).",
    )
    parser.add_argument(
        "-l",
        "--limit-per-TiB",
        type=float,
        default=DEFAULT_LIMIT_PER_TIB,
        help="Directories with fewer descendants per tebibyte than this will not "
        f"be descended into, default {DEFAULT_LIMIT_PER_TIB}",
    )
    parser.add_argument(
        "-P", "--no-progress", action="store_true", help="Do not show progress bars"
    )
    return parser


def parse_args(args=None):
    parser = make_parser()
    return parser.parse_args(args)


def main(args=None):
    args = parse_args(args)
    g = sizes_to_graph(args.input, args.total, not args.no_progress)
    with ensure_file(args.output, "w") as f:
        for fpath in yield_zips(g, progress=not args.no_progress):
            f.write(f"{os.fspath(fpath)}\n")


if __name__ == "__main__":
    main()
    # for s, b in [
    #     ("10", 10),
    #     ("10kB", 10 * 1000),
    #     ("10KiB", 10 * 1024),
    # ]:
    #     assert interpret_bytes(s) == b