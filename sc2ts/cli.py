import json
import collections
import concurrent.futures as cf
import logging
import itertools
import platform
import pathlib
import sys
import contextlib
import dataclasses
import datetime
import time
import os
from typing import List

import numpy as np
import tqdm
import tskit
import tszip
import tsinfer
import click
import humanize
import pandas as pd

try:
    import resource
except ImportError:
    resource = None  # resource.getrusage absent on windows, so skip outputting max mem

import sc2ts
from . import core
from . import utils
from . import info

logger = logging.getLogger(__name__)

__before = time.time()


def get_resources():
    # Measure all times in seconds
    wall_time = time.time() - __before
    os_times = os.times()
    user_time = os_times.user + os_times.children_user
    sys_time = os_times.system + os_times.children_system
    if resource is None:
        # Don't report max memory on Windows. We could do this using the psutil lib, via
        # psutil.Process(os.getpid()).get_ext_memory_info().peak_wset if demand exists
        maxmem = -1
    else:
        max_mem = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        if sys.platform != "darwin":
            max_mem *= 1024  # Linux and other OSs (e.g. freeBSD) report maxrss in kb
    return {
        "elapsed_time": wall_time,
        "user_time": user_time,
        "sys_time": sys_time,
        "max_memory": max_mem,  # bytes
    }


def summarise_usage():
    d = get_resources()
    # Report times in minutes
    wall_time = d["elapsed_time"] / 60
    user_time = d["user_time"] / 60
    sys_time = d["sys_time"] / 60
    max_mem = d["max_memory"]
    if max_mem > 0:
        maxmem_str = "; max_memory=" + humanize.naturalsize(max_mem, binary=True)
    return f"elapsed={wall_time:.2f}m; user={user_time:.2f}m; sys={sys_time:.2f}m{maxmem_str}"


def get_environment():
    """
    Returns a dictionary describing the environment in which sc2ts
    is currently running.
    """
    env = {
        "os": {
            "system": platform.system(),
            "node": platform.node(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
        },
        "python": {
            "implementation": platform.python_implementation(),
            "version": platform.python_version(),
        },
        "libraries": {
            "tsinfer": {"version": tsinfer.__version__},
            "tskit": {"version": tskit.__version__},
        },
    }
    return env


def get_provenance_dict():
    """
    Returns a dictionary encoding an execution of stdpopsim conforming to the
    tskit provenance schema.
    """
    document = {
        "schema_version": "1.0.0",
        "software": {"name": "sc2ts", "version": core.__version__},
        "parameters": {"command": sys.argv[0], "args": sys.argv[1:]},
        "environment": get_environment(),
        "resources": get_resources(),
    }
    return document


def setup_logging(verbosity, log_file=None):
    log_level = "WARN"
    if verbosity > 0:
        log_level = "INFO"
    if verbosity > 1:
        log_level = "DEBUG"
    handler = logging.StreamHandler()
    if log_file is not None:
        handler = logging.FileHandler(log_file)
    # default time format has millisecond precision which we don't need
    time_format = "%Y-%m-%d %H:%M:%S"
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", datefmt=time_format
    )
    handler.setFormatter(fmt)

    # This is mainly used to output messages about major events. Possibly
    # should do this with a separate logger entirely, rather than use
    # the "WARNING" channel.
    warn_handler = logging.StreamHandler()
    warn_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    warn_handler.setLevel(logging.WARN)

    for name in ["sc2ts", "tsinfer.inference"]:
        logger = logging.getLogger(name)
        logger.setLevel(log_level)
        logger.addHandler(handler)
        logger.addHandler(warn_handler)


# TODO add options to list keys, dump specific alignments etc
@click.command()
@click.argument("store", type=click.Path(exists=True, dir_okay=False))
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def info_alignments(store, verbose, log_file):
    """
    Information about an alignment store
    """
    setup_logging(verbose, log_file)
    with sc2ts.AlignmentStore(store) as alignment_store:
        print(alignment_store)


@click.command()
@click.argument("store", type=click.Path(dir_okay=False, file_okay=True))
@click.argument("fastas", type=click.Path(exists=True, dir_okay=False), nargs=-1)
@click.option("-i", "--initialise", default=False, type=bool, help="Initialise store")
@click.option("--no-progress", default=False, type=bool, help="Don't show progress")
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def import_alignments(store, fastas, initialise, no_progress, verbose, log_file):
    """
    Import the alignments from all FASTAS into STORE.
    """
    setup_logging(verbose, log_file)
    if initialise:
        a = sc2ts.AlignmentStore.initialise(store)
    else:
        a = sc2ts.AlignmentStore(store, "a")
    for fasta_path in fastas:
        logging.info(f"Reading fasta {fasta_path}")
        fasta = core.FastaReader(fasta_path)
        a.append(fasta, show_progress=True)
    a.close()


@click.command()
@click.argument("metadata")
@click.argument("db")
@click.option("-v", "--verbose", count=True)
def import_metadata(metadata, db, verbose):
    """
    Convert a CSV formatted metadata file to a database for later use.
    """
    setup_logging(verbose)
    sc2ts.MetadataDb.import_csv(metadata, db)


@click.command()
@click.argument("metadata", type=click.Path(exists=True, dir_okay=False))
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def info_metadata(metadata, verbose, log_file):
    """
    Information about a metadata DB
    """
    setup_logging(verbose, log_file)
    with sc2ts.MetadataDb(metadata) as metadata_db:
        print(metadata_db)


@click.command()
@click.argument("match_db", type=click.Path(exists=True, dir_okay=False))
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def info_matches(match_db, verbose, log_file):
    """
    Information about an alignment store
    """
    setup_logging(verbose, log_file)
    with sc2ts.MatchDb(match_db) as db:
        print(db)
        print("last date = ", db.last_date())
        print("cost\tpercent\tcount")
        df = db.as_dataframe()
        total = len(db)
        hmm_cost_counter = collections.Counter(df["hmm_cost"].astype(int))
        for cost in sorted(hmm_cost_counter.keys()):
            count = hmm_cost_counter[cost]
            percent = count / total * 100
            print(f"{cost}\t{percent:.1f}\t{count}")


@click.command()
@click.argument("ts_path", type=click.Path(exists=True, dir_okay=False))
@click.option("-R", "--recombinants", is_flag=True)
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def info_ts(ts_path, recombinants, verbose, log_file):
    """
    Information about a sc2ts inferred ARG
    """
    setup_logging(verbose, log_file)
    ts = tszip.load(ts_path)

    ti = sc2ts.TreeInfo(ts, quick=False)
    # print("info", ti.node_counts())
    # TODO output these as TSVs rather than using pandas display?
    pd.set_option("display.max_rows", 500)
    pd.set_option("display.max_columns", 500)
    pd.set_option("display.width", 1000)
    print(ti.summary())
    # TODO more
    if recombinants:
        print(ti.recombinants_summary())


def add_provenance(ts, output_file):
    # Record provenance here because this is where the arguments are provided.
    provenance = get_provenance_dict()
    tables = ts.dump_tables()
    tables.provenances.add_row(json.dumps(provenance))
    tables.dump(output_file)
    logger.info(f"Wrote {output_file}")


@click.command()
@click.argument("ts", type=click.Path(dir_okay=False))
@click.argument("match_db", type=click.Path(dir_okay=False))
@click.option(
    "--problematic-sites",
    default=None,
    type=click.Path(exists=True, dir_okay=False),
    help=(
        "File containing the list of problematic sites to exclude. "
        "Note this is combined with the sites defined by --mask-flanks "
        "and --mask-problematic-regions options"
    ),
)
@click.option(
    "--mask-flanks",
    is_flag=True,
    flag_value=True,
    help=(
        "If true, add the non-genic regions at either end of the genome to "
        "problematic sites"
    ),
)
@click.option(
    "--mask-problematic-regions",
    is_flag=True,
    flag_value=True,
    help=("If true, add the problematic regions problematic sites"),
)
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def initialise(
    ts,
    match_db,
    problematic_sites,
    mask_flanks,
    mask_problematic_regions,
    verbose,
    log_file,
):
    """
    Initialise a new base tree sequence to begin inference.
    """
    setup_logging(verbose, log_file)

    problematic = np.array([], dtype=int)
    if problematic_sites is not None:
        problematic = np.loadtxt(problematic_sites, ndmin=1).astype(int)
        logger.info(f"Loaded {len(problematic)} problematic sites")
    if mask_flanks:
        flanks = core.get_flank_coordinates()
        logger.info(f"Masking {len(flanks)} sites in flanks")
        problematic = np.concatenate((flanks, problematic))
    if mask_problematic_regions:
        known_regions = core.get_problematic_regions()
        logger.info(f"Masking {len(known_regions)} sites in known problematic regions")
        problematic = np.concatenate((known_regions, problematic))

    base_ts = sc2ts.initial_ts(np.unique(problematic))
    add_provenance(base_ts, ts)
    logger.info(f"New base ts at {ts}")
    sc2ts.MatchDb.initialise(match_db)


@click.command()
@click.argument("metadata", type=click.Path(exists=True, dir_okay=False))
@click.option("--counts/--no-counts", default=False)
@click.option(
    "--after", default="1900-01-01", help="show dates equal to or after the specified value"
)
@click.option(
    "--before", default="3000-01-01", help="show dates before the specified value"
)
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def list_dates(metadata, counts, after, before, verbose, log_file):
    """
    List the dates included in specified metadataDB
    """
    setup_logging(verbose, log_file)
    with sc2ts.MetadataDb(metadata) as metadata_db:
        counter = metadata_db.date_sample_counts()
        for k in counter:
            if after <= k < before:
                if counts:
                    print(k, counter[k], sep="\t")
                else:
                    print(k)


def summarise_base(ts, date, progress):
    ti = sc2ts.TreeInfo(ts, quick=True)
    node_info = "; ".join(f"{k}:{v}" for k, v in ti.node_counts().items())
    logger.info(f"Loaded {node_info}")
    if progress:
        print(f"{date} Start base: {node_info}", file=sys.stderr)


@click.command()
@click.argument("base_ts", type=click.Path(exists=True, dir_okay=False))
@click.argument("date")
@click.argument("alignments", type=click.Path(exists=True, dir_okay=False))
@click.argument("metadata", type=click.Path(exists=True, dir_okay=False))
@click.argument("matches", type=click.Path(exists=True, dir_okay=False))
@click.argument("output_ts", type=click.Path(dir_okay=False))
@click.option(
    "--num-mismatches",
    default=3,
    show_default=True,
    type=float,
    help="Number of mismatches to accept in favour of recombination",
)
@click.option(
    "--hmm-cost-threshold",
    default=5,
    type=float,
    show_default=True,
    help="The maximum HMM cost for samples to be included unconditionally",
)
@click.option(
    "--min-group-size",
    default=10,
    show_default=True,
    type=int,
    help="Minimum size of groups of reconsidered samples for inclusion",
)
@click.option(
    "--min-root-mutations",
    default=2,
    show_default=True,
    type=int,
    help="Minimum number of shared mutations for reconsidered sample groups",
)
@click.option(
    "--max-mutations-per-sample",
    default=10,
    show_default=True,
    type=int,
    help=(
        "Maximum average number of mutations per sample in an inferred retrospective "
        "group tree"
    ),
)
@click.option(
    "--max-recurrent-mutations",
    default=10,
    show_default=True,
    type=int,
    help=(
        "Maximum number of recurrent mutations in an inferred retrospective "
        "group tree"
    ),
)
@click.option(
    "--retrospective-window",
    default=30,
    show_default=True,
    type=int,
    help="Number of days in the past to reconsider potential matches",
)
@click.option(
    "--deletions-as-missing/--no-deletions-as-missing",
    default=True,
    help="Treat all deletions as missing data when matching haplotypes",
    show_default=True,
)
@click.option(
    "--max-daily-samples",
    default=None,
    type=int,
    help=(
        "The maximum number of samples to match in a single day. If the total "
        "is greater than this, randomly subsample."
    ),
)
@click.option(
    "--max-missing-sites",
    default=None,
    type=int,
    help=(
        "The maximum number of missing sites in a sample to be accepted for inclusion"
    ),
)
@click.option(
    "--random-seed",
    default=42,
    type=int,
    help="Random seed for subsampling",
    show_default=True,
)
@click.option(
    "--num-threads",
    default=0,
    type=int,
    help="Number of match threads (default to one)",
)
@click.option("--progress/--no-progress", default=True)
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
@click.option(
    "-f",
    "--force",
    is_flag=True,
    flag_value=True,
    help="Force clearing newer matches from DB",
)
def extend(
    base_ts,
    date,
    alignments,
    metadata,
    matches,
    output_ts,
    num_mismatches,
    hmm_cost_threshold,
    min_group_size,
    min_root_mutations,
    max_mutations_per_sample,
    max_recurrent_mutations,
    retrospective_window,
    deletions_as_missing,
    max_daily_samples,
    max_missing_sites,
    num_threads,
    random_seed,
    progress,
    verbose,
    log_file,
    force,
):
    """
    Extend base_ts with sequences for the specified date, using specified
    alignments and metadata databases, updating the specified matches
    database, and outputting the result to the specified file.
    """
    setup_logging(verbose, log_file)
    base = tskit.load(base_ts)
    summarise_base(base, date, progress)
    with contextlib.ExitStack() as exit_stack:
        alignment_store = exit_stack.enter_context(sc2ts.AlignmentStore(alignments))
        metadata_db = exit_stack.enter_context(sc2ts.MetadataDb(metadata))
        match_db = exit_stack.enter_context(sc2ts.MatchDb(matches))

        newer_matches = match_db.count_newer(date)
        if newer_matches > 0:
            if not force:
                click.confirm(
                    f"Do you want to remove {newer_matches} newer matches "
                    f"from MatchDB > {date}?",
                    abort=True,
                )
                match_db.delete_newer(date)
        ts_out = sc2ts.extend(
            alignment_store=alignment_store,
            metadata_db=metadata_db,
            base_ts=base,
            date=date,
            match_db=match_db,
            num_mismatches=num_mismatches,
            hmm_cost_threshold=hmm_cost_threshold,
            min_group_size=min_group_size,
            min_root_mutations=min_root_mutations,
            max_mutations_per_sample=max_mutations_per_sample,
            max_recurrent_mutations=max_recurrent_mutations,
            retrospective_window=retrospective_window,
            deletions_as_missing=deletions_as_missing,
            max_daily_samples=max_daily_samples,
            max_missing_sites=max_missing_sites,
            random_seed=random_seed,
            num_threads=num_threads,
            show_progress=progress,
        )
        add_provenance(ts_out, output_ts)
    resource_usage = f"{date}:{summarise_usage()}"
    logger.info(resource_usage)
    if progress:
        print(resource_usage, file=sys.stderr)


@click.command()
@click.argument("alignment_db")
@click.argument("ts_file")
@click.option(
    "--deletions-as-missing/--no-deletions-as-missing",
    default=True,
    help="Treat all deletions as missing data when matching haplotypes",
    show_default=True,
)
@click.option("-v", "--verbose", count=True)
def validate(alignment_db, ts_file, deletions_as_missing, verbose):
    """
    Check that the specified trees correctly encode alignments for samples.
    """
    setup_logging(verbose)

    ts = tszip.load(ts_file)
    with sc2ts.AlignmentStore(alignment_db) as alignment_store:
        sc2ts.validate(ts, alignment_store, deletions_as_missing, show_progress=True)


@click.command()
@click.argument("ts_file")
@click.option("-v", "--verbose", count=True)
def export_alignments(ts_file, verbose):
    """
    Export alignments from the specified tskit file to FASTA
    """
    setup_logging(verbose)
    ts = tszip.load(ts_file)
    for u, alignment in zip(ts.samples(), ts.alignments(left=1)):
        strain = ts.node(u).metadata["strain"]
        if strain == core.REFERENCE_STRAIN:
            continue
        print(f">{strain}")
        print(alignment)


@click.command()
@click.argument("ts_file")
@click.option("-v", "--verbose", count=True)
def export_metadata(ts_file, verbose):
    """
    Export metadata from the specified tskit file to TSV
    """
    setup_logging(verbose)
    ts = tszip.load(ts_file)
    data = []
    for u in ts.samples():
        md = ts.node(u).metadata
        if md["strain"] == core.REFERENCE_STRAIN:
            continue
        try:
            # FIXME this try/except is needed because of some samples not having full
            # metadata. Can drop when fixed.
            del md["sc2ts"]
        except KeyError:
            pass
        data.append(md)
    df = pd.DataFrame(data)
    df.to_csv(sys.stdout, sep="\t", index=False)


@click.command()
@click.argument("ts", type=click.Path(exists=True, dir_okay=False))
@click.argument("metadata", type=click.Path(exists=True, dir_okay=False))
@click.option("-v", "--verbose", count=True)
def tally_lineages(ts, metadata, verbose):
    """
    Output a table in TSV format comparing the number of samples associated
    each pango lineage in the ARG along with the corresponding number in
    the metadata DB.
    """
    setup_logging(verbose)
    ts = tszip.load(ts)
    with sc2ts.MetadataDb(metadata) as metadata_db:
        df = info.tally_lineages(ts, metadata_db, show_progress=True)
    df.to_csv(sys.stdout, sep="\t", index=False)


@dataclasses.dataclass(frozen=True)
class HmmRun:
    strain: str
    num_mismatches: int
    direction: str
    match: sc2ts.HmmMatch

    def asdict(self):
        d = dataclasses.asdict(self)
        d["match"] = dataclasses.asdict(self.match)
        return d

    def asjson(self):
        return json.dumps(self.asdict())


@dataclasses.dataclass(frozen=True)
class MatchWork:
    ts_path: str
    samples: List
    num_mismatches: int
    direction: str


def _match_worker(work):
    msg = (
        f"k={work.num_mismatches} n={len(work.samples)} "
        f"{work.direction} {work.ts_path}"
    )
    logger.info(f"Start: {msg}")
    ts = tszip.load(work.ts_path)
    mu, rho = sc2ts.solve_num_mismatches(work.num_mismatches)
    matches = sc2ts.match_tsinfer(
        samples=work.samples,
        ts=ts,
        mu=mu,
        rho=rho,
        num_threads=0,
        show_progress=False,
        # Maximum possible precision
        likelihood_threshold=1e-200,
        mirror_coordinates=work.direction == "reverse",
    )
    runs = []
    for hmm_match, sample in zip(matches, work.samples):
        runs.append(
            HmmRun(
                strain=sample.strain,
                num_mismatches=work.num_mismatches,
                direction=work.direction,
                match=hmm_match,
            )
        )
    logger.info(f"Finish: {msg}")
    return runs


@click.command()
@click.argument("alignments_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("ts_path", type=click.Path(exists=True, dir_okay=False))
@click.argument("strains", nargs=-1)
@click.option("--num-mismatches", default=3, type=int, help="num-mismatches")
@click.option(
    "--direction",
    type=click.Choice(["forward", "reverse"]),
    default="forward",
    help="Direction to run HMM in",
)
@click.option(
    "--num-threads",
    default=0,
    type=int,
    help="Number of match threads (default to one)",
)
@click.option("--progress/--no-progress", default=True)
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def run_match(
    alignments_path,
    ts_path,
    strains,
    num_mismatches,
    direction,
    num_threads,
    progress,
    verbose,
    log_file,
):
    """
    Run matches for a specified set of strains, outputting details to stdout as JSON.
    """
    setup_logging(verbose, log_file)
    ts = tszip.load(ts_path)
    if len(strains) == 0:
        return
    progress_title = "Match"
    samples = sc2ts.preprocess(
        list(strains),
        alignments_path,
        show_progress=progress,
        progress_title=progress_title,
        keep_sites=ts.sites_position.astype(int),
        num_workers=num_threads,
    )
    for sample in samples:
        if sample.haplotype is None:
            raise ValueError(f"No alignment stored for {sample.strain}")

    mu, rho = sc2ts.solve_num_mismatches(num_mismatches)
    matches = sc2ts.match_tsinfer(
        samples=samples,
        ts=ts,
        mu=mu,
        rho=rho,
        num_threads=num_threads,
        show_progress=progress,
        progress_title=progress_title,
        progress_phase="HMM",
        # Maximum possible precision
        likelihood_threshold=1e-200,
        mirror_coordinates=direction == "reverse",
    )
    for hmm_match, sample in zip(matches, samples):
        run = HmmRun(
            strain=sample.strain,
            num_mismatches=num_mismatches,
            direction=direction,
            match=hmm_match,
        )
        print(run.asjson())


def find_previous_date_path(date, path_pattern):
    """
    Find the path with the most-recent date to the specified one
    matching the given pattern.
    """
    date = datetime.date.fromisoformat(date)
    for j in range(1, 30):
        previous_date = date - datetime.timedelta(days=j)
        path = pathlib.Path(path_pattern.format(previous_date))
        logger.debug(f"Trying {path}")
        if path.exists():
            break
    else:
        raise ValueError(
            f"No path exists for pattern {path_pattern} starting at {date}"
        )
    return path


@click.command()
@click.argument("alignments", type=click.Path(exists=True, dir_okay=False))
@click.argument("ts", type=click.Path(exists=True, dir_okay=False))
@click.argument("path_pattern")
@click.option(
    "-k",
    "--num-mismatches",
    default=[3],
    type=int,
    multiple=True,
    help="num-mismatches",
)
@click.option(
    "--num-threads",
    default=0,
    type=int,
    help="Number of match threads (default to one)",
)
@click.option("--progress/--no-progress", default=True)
@click.option("-v", "--verbose", count=True)
@click.option("-l", "--log-file", default=None, type=click.Path(dir_okay=False))
def run_rematch_recombinants(
    alignments,
    ts,
    path_pattern,
    num_mismatches,
    num_threads,
    progress,
    verbose,
    log_file,
):
    setup_logging(verbose, log_file)
    ts = tszip.load(ts)
    # This is a map of recombinant node to the samples involved in
    # the original causal sample group.
    recombinant_strains = sc2ts.get_recombinant_strains(ts)
    logger.info(
        f"Got {len(recombinant_strains)} recombinants and "
        f"{sum(len(v) for v in recombinant_strains.values())} strains"
    )

    # Map recombinants to originating date
    recombinant_to_path = {}
    strain_to_recombinant = {}
    all_strains = []
    for u, strains in recombinant_strains.items():
        date_added = ts.node(u).metadata["sc2ts"]["date_added"]
        base_ts_path = find_previous_date_path(date_added, path_pattern)
        recombinant_to_path[u] = base_ts_path
        for strain in strains:
            strain_to_recombinant[strain] = u
            all_strains.append(strain)

    progress_title = "Recomb"
    samples = sc2ts.preprocess(
        all_strains,
        alignments,
        show_progress=progress,
        progress_title=progress_title,
        keep_sites=ts.sites_position.astype(int),
        num_workers=num_threads,
    )

    recombinant_to_samples = collections.defaultdict(list)
    for sample in samples:
        if sample.haplotype is None:
            raise ValueError(f"No alignment stored for {sample.strain}")
        recombinant = strain_to_recombinant[sample.strain]
        recombinant_to_samples[recombinant].append(sample)

    work = []
    for recombinant, samples in recombinant_to_samples.items():
        for direction in ["forward", "reverse"]:
            for k in num_mismatches:
                work.append(
                    MatchWork(
                        recombinant_to_path[recombinant],
                        samples,
                        num_mismatches=k,
                        direction=direction,
                    )
                )

    bar = sc2ts.get_progress(None, progress_title, "HMM", progress, total=len(work))

    def output(hmm_runs):
        bar.update()
        for run in hmm_runs:
            print(run.asjson())

    results = []
    if num_threads == 0:
        for w in work:
            hmm_runs = _match_worker(w)
            output(hmm_runs)
    else:
        with cf.ProcessPoolExecutor(num_threads) as executor:
            futures = [executor.submit(_match_worker, w) for w in work]
            for future in cf.as_completed(futures):
                hmm_runs = future.result()
                output(hmm_runs)
    bar.close()


@click.version_option(core.__version__)
@click.group()
def cli():
    pass


cli.add_command(import_alignments)
cli.add_command(import_metadata)
cli.add_command(info_alignments)
cli.add_command(info_metadata)
cli.add_command(info_matches)
cli.add_command(info_ts)
cli.add_command(export_alignments)
cli.add_command(export_metadata)

cli.add_command(initialise)
cli.add_command(list_dates)
cli.add_command(extend)
cli.add_command(validate)
cli.add_command(run_match)
cli.add_command(run_rematch_recombinants)
cli.add_command(tally_lineages)
