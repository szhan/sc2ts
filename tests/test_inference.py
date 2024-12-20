import collections
import hashlib
import logging

import numpy as np
import numpy.testing as nt
import pytest
import tsinfer
import tskit
import msprime
import pandas as pd

import sc2ts
import util


def recombinant_example_1(ts_map):
    """
    Example recombinant created by cherry picking two samples that differ
    by mutations on either end of the genome, and smushing them together.
    Note there's only two mutations needed, so we need to set num_mismatches=2
    """
    ts = ts_map["2020-02-13"]
    strains = ["SRR11597188", "SRR11597163"]
    nodes = [
        ts.samples()[ts.metadata["sc2ts"]["samples_strain"].index(strain)]
        for strain in strains
    ]
    assert nodes == [31, 45]
    # Site positions
    # SRR11597188 36  [(871, 'G'), (3027, 'G'), (3787, 'T')]
    # SRR11597163 51  [(15324, 'T'), (29303, 'T')]
    H = ts.genotype_matrix(samples=nodes, alleles=tuple("ACGT-")).T
    bp = 10_000
    h = H[0].copy()
    h[bp:] = H[1][bp:]

    s = sc2ts.Sample("frankentype", "2020-02-14", haplotype=h)
    return ts, s


def tmp_alignment_store(tmp_path, alignments):
    path = tmp_path / "synthetic_alignments.db"
    alignment_db = sc2ts.AlignmentStore(path, mode="rw")
    alignment_db.append(alignments)
    return alignment_db


def tmp_metadata_db(tmp_path, strains, date):
    data = []
    for strain in strains:
        data.append({"strain": strain, "date": date})
    df = pd.DataFrame(data)
    csv_path = tmp_path / "metadata.csv"
    df.to_csv(csv_path)
    db_path = tmp_path / "metadata.db"
    sc2ts.MetadataDb.import_csv(csv_path, db_path, sep=",")
    return sc2ts.MetadataDb(db_path)


def test_get_group_strains(fx_ts_map):
    ts = fx_ts_map["2020-02-13"]
    groups = sc2ts.get_group_strains(ts)
    assert len(groups) > 0
    for group_id, strains in groups.items():
        m = hashlib.md5()
        for strain in sorted(strains):
            m.update(strain.encode())
        assert group_id == m.hexdigest()


class TestRecombinantHandling:

    def test_get_recombinant_strains_ex1(self, fx_recombinant_example_1):
        d = sc2ts.get_recombinant_strains(fx_recombinant_example_1)
        assert d == {55: ["recombinant_example_1_0", "recombinant_example_1_1"]}

    def test_get_recombinant_strains_ex2(self, fx_recombinant_example_2):
        d = sc2ts.get_recombinant_strains(fx_recombinant_example_2)
        assert d == {56: ["recombinant"]}


class TestSolveNumMismatches:
    @pytest.mark.parametrize(
        ["k", "expected_rho"],
        [(2, 0.0001904), (3, 2.50582e-06), (4, 3.297146e-08), (1000, 0)],
    )
    def test_examples(self, k, expected_rho):
        mu, rho = sc2ts.solve_num_mismatches(k)
        assert mu == 0.0125
        nt.assert_almost_equal(rho, expected_rho)


class TestInitialTs:
    def test_reference_sequence(self):
        ts = sc2ts.initial_ts()
        assert ts.reference_sequence.metadata["genbank_id"] == "MN908947"
        assert ts.reference_sequence.data == sc2ts.core.get_reference_sequence()

    def test_reference_sample(self):
        ts = sc2ts.initial_ts()
        assert ts.num_samples == 1
        node = ts.node(ts.samples()[0])
        assert node.time == 0
        assert node.metadata == {
            "date": "2019-12-26",
            "strain": "Wuhan/Hu-1/2019",
            "sc2ts": {"notes": "Reference sequence"},
        }
        alignment = next(ts.alignments())
        assert alignment == sc2ts.core.get_reference_sequence()


class TestMatchTsinfer:
    def match_tsinfer(self, samples, ts, mirror_coordinates=False, **kwargs):
        return sc2ts.inference.match_tsinfer(
            samples=samples,
            ts=ts,
            mu=0.125,
            rho=0,
            mirror_coordinates=mirror_coordinates,
            **kwargs,
        )

    @pytest.mark.parametrize("mirror", [False, True])
    def test_match_reference(self, mirror):
        ts = sc2ts.initial_ts()
        tables = ts.dump_tables()
        tables.sites.truncate(20)
        ts = tables.tree_sequence()
        alignment = sc2ts.core.get_reference_sequence(as_array=True)
        a = sc2ts.encode_alignment(alignment)
        h = a[ts.sites_position.astype(int)]
        samples = [sc2ts.Sample("test", "2020-01-01", haplotype=h)]
        matches = self.match_tsinfer(samples, ts, mirror_coordinates=mirror)
        assert matches[0].breakpoints == [0, ts.sequence_length]
        assert matches[0].parents == [ts.num_nodes - 1]
        assert len(matches[0].mutations) == 0

    @pytest.mark.parametrize("mirror", [False, True])
    @pytest.mark.parametrize("site_id", [0, 10, 19])
    def test_match_reference_one_mutation(self, mirror, site_id):
        ts = sc2ts.initial_ts()
        tables = ts.dump_tables()
        tables.sites.truncate(20)
        ts = tables.tree_sequence()
        alignment = sc2ts.core.get_reference_sequence(as_array=True)
        a = sc2ts.encode_alignment(alignment)
        h = a[ts.sites_position.astype(int)]
        samples = [sc2ts.Sample("test", "2020-01-01", haplotype=h)]
        # Mutate to gap
        h[site_id] = sc2ts.core.ALLELES.index("-")
        matches = self.match_tsinfer(samples, ts, mirror_coordinates=mirror)
        assert matches[0].breakpoints == [0, ts.sequence_length]
        assert matches[0].parents == [ts.num_nodes - 1]
        assert len(matches[0].mutations) == 1
        mut = matches[0].mutations[0]
        assert mut.site_id == site_id
        assert mut.site_position == ts.sites_position[site_id]
        assert mut.derived_state == "-"
        assert mut.inherited_state == ts.site(site_id).ancestral_state
        assert not mut.is_reversion
        assert not mut.is_immediate_reversion

    @pytest.mark.parametrize("mirror", [False, True])
    @pytest.mark.parametrize("allele", range(5))
    def test_match_reference_all_same(self, mirror, allele):
        ts = sc2ts.initial_ts()
        tables = ts.dump_tables()
        tables.sites.truncate(20)
        ts = tables.tree_sequence()
        alignment = sc2ts.core.get_reference_sequence(as_array=True)
        a = sc2ts.encode_alignment(alignment)
        ref = a[ts.sites_position.astype(int)]
        h = np.zeros_like(ref) + allele
        samples = [sc2ts.Sample("test", "2020-01-01", haplotype=h)]
        matches = self.match_tsinfer(samples, ts, mirror_coordinates=mirror)
        assert matches[0].breakpoints == [0, ts.sequence_length]
        assert matches[0].parents == [ts.num_nodes - 1]
        muts = matches[0].mutations
        assert len(muts) > 0
        assert len(muts) == np.sum(ref != allele)
        for site_id, mut in zip(np.where(ref != allele)[0], muts):
            assert mut.site_id == site_id
            assert mut.derived_state == sc2ts.core.ALLELES[allele]


class TestMirrorTsCoords:
    def test_dense_sites_example(self):
        tree = tskit.Tree.generate_balanced(2, span=10)
        tables = tree.tree_sequence.dump_tables()
        tables.sites.add_row(0, "A")
        tables.sites.add_row(2, "C")
        tables.sites.add_row(5, "-")
        tables.sites.add_row(8, "G")
        tables.sites.add_row(9, "T")
        ts1 = tables.tree_sequence()
        ts2 = sc2ts.inference.mirror_ts_coordinates(ts1)
        assert ts2.num_sites == ts1.num_sites
        assert list(ts2.sites_position) == [0, 1, 4, 7, 9]
        assert "".join(site.ancestral_state for site in ts2.sites()) == "TG-CA"

    def test_sparse_sites_example(self):
        tree = tskit.Tree.generate_balanced(2, span=100)
        tables = tree.tree_sequence.dump_tables()
        tables.sites.add_row(10, "A")
        tables.sites.add_row(12, "C")
        tables.sites.add_row(15, "-")
        tables.sites.add_row(18, "G")
        tables.sites.add_row(19, "T")
        ts1 = tables.tree_sequence()
        ts2 = sc2ts.inference.mirror_ts_coordinates(ts1)
        assert ts2.num_sites == ts1.num_sites
        assert list(ts2.sites_position) == [80, 81, 84, 87, 89]
        assert "".join(site.ancestral_state for site in ts2.sites()) == "TG-CA"

    def check_double_mirror(self, ts):
        mirror = sc2ts.inference.mirror_ts_coordinates(ts)
        for h1, h2 in zip(ts.haplotypes(), mirror.haplotypes()):
            assert h1 == h2[::-1]
        double_mirror = sc2ts.inference.mirror_ts_coordinates(mirror)
        ts.tables.assert_equals(double_mirror.tables)

    @pytest.mark.parametrize("n", [2, 3, 13, 20])
    def test_single_tree_no_mutations(self, n):
        ts = msprime.sim_ancestry(n, random_seed=42)
        self.check_double_mirror(ts)

    @pytest.mark.parametrize("n", [2, 3, 13, 20])
    def test_multiple_trees_no_mutations(self, n):
        ts = msprime.sim_ancestry(
            n,
            sequence_length=100,
            recombination_rate=1,
            random_seed=420,
        )
        assert ts.num_trees > 1
        self.check_double_mirror(ts)

    @pytest.mark.parametrize("n", [2, 3, 13, 20])
    def test_single_tree_mutations(self, n):
        ts = msprime.sim_ancestry(n, sequence_length=100, random_seed=42234)
        ts = msprime.sim_mutations(ts, rate=0.01, random_seed=32234)
        assert ts.num_sites > 2
        self.check_double_mirror(ts)

    @pytest.mark.parametrize("n", [2, 3, 13, 20])
    def test_multiple_tree_mutations(self, n):
        ts = msprime.sim_ancestry(
            n, sequence_length=100, recombination_rate=0.1, random_seed=1234
        )
        ts = msprime.sim_mutations(ts, rate=0.1, random_seed=334)
        assert ts.num_sites > 2
        assert ts.num_trees > 2
        self.check_double_mirror(ts)

    def test_high_recomb_mutation(self):
        # Example that's saturated for muts and recombs
        ts = msprime.sim_ancestry(
            10, sequence_length=10, recombination_rate=10, random_seed=1
        )
        assert ts.num_trees == 10
        ts = msprime.sim_mutations(ts, rate=1, random_seed=1)
        assert ts.num_sites == 10
        assert ts.num_mutations > 10
        self.check_double_mirror(ts)


class TestRealData:
    dates = [
        "2020-01-01",
        "2020-01-19",
        "2020-01-24",
        "2020-01-25",
        "2020-01-28",
        "2020-01-29",
        "2020-01-30",
        "2020-01-31",
        "2020-02-01",
        "2020-02-02",
        "2020-02-03",
        "2020-02-04",
        "2020-02-05",
        "2020-02-06",
        "2020-02-07",
        "2020-02-08",
        "2020-02-09",
        "2020-02-10",
        "2020-02-11",
        "2020-02-13",
    ]

    def test_first_day(self, tmp_path, fx_ts_map, fx_alignment_store, fx_metadata_db):
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map[self.dates[0]],
            date="2020-01-19",
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )
        # 25.00┊ 0 ┊
        #      ┊ ┃ ┊
        # 24.00┊ 1 ┊
        #      ┊ ┃ ┊
        # 0.00 ┊ 2 ┊
        #     0 29904
        assert ts.num_trees == 1
        assert ts.num_nodes == 3
        assert ts.num_samples == 2
        assert ts.num_mutations == 3
        assert list(ts.nodes_time) == [25, 24, 0]
        assert ts.metadata["sc2ts"]["date"] == "2020-01-19"
        assert ts.metadata["sc2ts"]["samples_strain"] == [
            "Wuhan/Hu-1/2019",
            "SRR11772659",
        ]
        assert list(ts.samples()) == [1, 2]
        assert ts.node(1).metadata["strain"] == "Wuhan/Hu-1/2019"
        assert ts.node(2).metadata["strain"] == "SRR11772659"
        assert list(ts.mutations_node) == [2, 2, 2]
        assert list(ts.mutations_time) == [0, 0, 0]
        assert list(ts.sites_position[ts.mutations_site]) == [8782, 18060, 28144]
        sc2ts_md = ts.node(2).metadata["sc2ts"]
        hmm_md = sc2ts_md["hmm_match"]
        assert len(hmm_md["mutations"]) == 3
        for mut_md, mut in zip(hmm_md["mutations"], ts.mutations()):
            assert mut_md["derived_state"] == mut.derived_state
            assert mut_md["site_position"] == ts.sites_position[mut.site]
            assert mut_md["inherited_state"] == ts.site(mut.site).ancestral_state
        assert hmm_md["path"] == [{"left": 0, "parent": 1, "right": 29904}]
        assert sc2ts_md["num_missing_sites"] == 121
        assert sc2ts_md["alignment_composition"] == {
            "A": 8893,
            "C": 5471,
            "G": 5849,
            "T": 9564,
            "N": 121,
        }
        assert sum(sc2ts_md["alignment_composition"].values()) == ts.num_sites
        ts.tables.assert_equals(fx_ts_map["2020-01-19"].tables, ignore_provenance=True)

    def test_2020_01_25(self, tmp_path, fx_ts_map, fx_alignment_store, fx_metadata_db):
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-01-24"],
            date="2020-01-25",
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )
        assert ts.num_samples == 5
        assert ts.metadata["sc2ts"]["exact_matches"]["pango"] == {"B": 2}
        assert ts.metadata["sc2ts"]["exact_matches"]["node"] == {"5": 2}
        ts.tables.assert_equals(fx_ts_map["2020-01-25"].tables, ignore_provenance=True)

    def test_2020_02_02(self, tmp_path, fx_ts_map, fx_alignment_store, fx_metadata_db):
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-02-01"],
            date="2020-02-02",
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )
        assert ts.num_samples == 22
        assert ts.metadata["sc2ts"]["exact_matches"]["pango"] == {"A": 2, "B": 2}
        assert np.sum(ts.nodes_time[ts.samples()] == 0) == 4

        ts.tables.assert_equals(fx_ts_map["2020-02-02"].tables, ignore_provenance=True)

    @pytest.mark.parametrize("max_samples", range(1, 6))
    def test_2020_02_02_max_samples(
        self, tmp_path, fx_ts_map, fx_alignment_store, fx_metadata_db, max_samples
    ):
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-02-01"],
            date="2020-02-02",
            max_daily_samples=max_samples,
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )
        new_samples = min(4, max_samples)
        assert ts.num_samples == 18 + new_samples
        assert np.sum(ts.nodes_time[ts.samples()] == 0) == new_samples

    def test_2020_02_02_max_missing_sites(
        self, tmp_path, fx_ts_map, fx_alignment_store, fx_metadata_db
    ):
        max_missing_sites = 123
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-02-01"],
            date="2020-02-02",
            max_missing_sites=max_missing_sites,
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )
        new_samples = 2
        assert ts.num_samples == 18 + new_samples

        assert np.sum(ts.nodes_time[ts.samples()] == 0) == new_samples
        for u in ts.samples()[-new_samples:]:
            assert (
                ts.node(u).metadata["sc2ts"]["num_missing_sites"] <= max_missing_sites
            )

    @pytest.mark.parametrize(
        ["strain", "start", "length"],
        [("SRR11597164", 1547, 1), ("SRR11597190", 3951, 3)],
    )
    @pytest.mark.parametrize("deletions_as_missing", [True, False])
    def test_2020_02_02_deletion_sample(
        self,
        tmp_path,
        fx_alignment_store,
        fx_metadata_db,
        fx_ts_map,
        strain,
        start,
        length,
        deletions_as_missing,
    ):
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-02-01"],
            date="2020-02-02",
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
            deletions_as_missing=deletions_as_missing,
        )
        u = ts.samples()[ts.metadata["sc2ts"]["samples_strain"].index(strain)]
        md = ts.node(u).metadata["sc2ts"]
        assert md["alignment_composition"]["-"] == length
        for j in range(length):
            site = ts.site(position=start + j)
            assert len(site.mutations) == 0 if deletions_as_missing else 1
            for mut in site.mutations:
                assert mut.node == u
                assert mut.derived_state == "-"
            # We pick the site up as somewhere with deletions regardless
            # of deletions_as_missing
            assert site.metadata["sc2ts"]["deletion_samples"] == 1

    @pytest.mark.parametrize(
        ["strain", "num_missing"], [("SRR11597164", 122), ("SRR11597114", 402)]
    )
    def test_2020_02_02_missing_sample(
        self,
        fx_ts_map,
        fx_alignment_store,
        strain,
        num_missing,
    ):
        alignment = fx_alignment_store[strain]
        a = sc2ts.encode_alignment(alignment)

        missing_positions = np.where(a == -1)[0][1:]
        assert len(missing_positions) == num_missing
        ts_prev = fx_ts_map["2020-02-01"]
        ts = fx_ts_map["2020-02-02"]
        u = ts.samples()[ts.metadata["sc2ts"]["samples_strain"].index(strain)]
        md = ts.node(u).metadata["sc2ts"]
        assert md["num_missing_sites"] == num_missing
        for pos in missing_positions:
            site = ts.site(position=pos)
            site_prev = ts_prev.site(position=pos)
            assert (
                site.metadata["sc2ts"]["missing_samples"]
                > site_prev.metadata["sc2ts"]["missing_samples"]
            )

    @pytest.mark.parametrize("deletions_as_missing", [True, False])
    def test_2020_02_02_deletions_as_missing(
        self,
        tmp_path,
        fx_ts_map,
        fx_alignment_store,
        fx_metadata_db,
        deletions_as_missing,
    ):
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-02-01"],
            date="2020-02-02",
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
            deletions_as_missing=deletions_as_missing,
        )
        ti = sc2ts.TreeInfo(ts, show_progress=False)
        expected = 0 if deletions_as_missing else 4
        assert np.sum(ti.mutations_derived_state == "-") == expected

    def test_2020_02_08(self, tmp_path, fx_ts_map, fx_alignment_store, fx_metadata_db):
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-02-07"],
            date="2020-02-08",
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )

        # SRR11597163 has a reversion (4923, 'C')
        # Site ID 4923 has position 5025
        for node in ts.nodes():
            if node.is_sample() and node.metadata["strain"] == "SRR11597163":
                break
        assert node.metadata["strain"] == "SRR11597163"
        scmd = node.metadata["sc2ts"]
        # We have a mutation from a mismatch
        assert scmd["hmm_match"]["mutations"] == [
            {"derived_state": "C", "inherited_state": "T", "site_position": 5025}
        ]
        # But no mutations above the node itself.
        assert np.sum(ts.mutations_node == node.id) == 0

        tree = ts.first()
        rp_node = ts.node(tree.parent(node.id))
        assert rp_node.flags == sc2ts.NODE_IS_REVERSION_PUSH
        assert rp_node.metadata["sc2ts"] == {
            "date_added": "2020-02-08",
            "sites": [5019],
        }
        ts.tables.assert_equals(fx_ts_map["2020-02-08"].tables, ignore_provenance=True)

        sib_sample = ts.node(tree.siblings(node.id)[0])
        assert sib_sample.metadata["strain"] == "SRR11597168"

        assert np.sum(ts.mutations_node == sib_sample.id) == 1
        mutation = ts.mutation(np.where(ts.mutations_node == sib_sample.id)[0][0])
        assert mutation.derived_state == "T"
        assert mutation.parent == -1

    def test_2020_02_14_all_matches(
        self, tmp_path, fx_ts_map, fx_alignment_store, fx_metadata_db, fx_match_db
    ):
        date = "2020-02-14"
        assert len(list(fx_metadata_db.get(date))) == 0
        ts = sc2ts.extend(
            alignment_store=fx_alignment_store,
            metadata_db=fx_metadata_db,
            base_ts=fx_ts_map["2020-02-13"],
            date="2020-02-15",
            match_db=fx_match_db,
            # This should allow everything in
            min_root_mutations=0,
            min_group_size=1,
            min_different_dates=1,
        )
        retro_groups = ts.metadata["sc2ts"]["retro_groups"]
        assert len(retro_groups) == 6
        assert retro_groups[0] == {
            "dates": ["2020-01-29"],
            "depth": 1,
            "group_id": "92312b65f8ec1eaf12de8218db67e737",
            "num_mutations": 19,
            "num_nodes": 2,
            "num_recurrent_mutations": 0,
            "num_root_mutations": 0,
            "pango_lineages": ["A.5"],
            "strains": ["SRR15736313"],
            "date_added": "2020-02-15",
        }

    def test_2020_02_14_skip_recurrent(
        self,
        tmp_path,
        fx_ts_map,
        fx_alignment_store,
        fx_metadata_db,
        fx_match_db,
        caplog,
    ):
        date = "2020-02-14"
        assert len(list(fx_metadata_db.get(date))) == 0
        with caplog.at_level("DEBUG", logger="sc2ts.inference"):
            ts = sc2ts.extend(
                alignment_store=fx_alignment_store,
                metadata_db=fx_metadata_db,
                base_ts=fx_ts_map["2020-02-13"],
                date="2020-02-15",
                match_db=fx_match_db,
                # This should allow everything in but exclude on max_recurrent
                min_root_mutations=0,
                min_group_size=1,
                min_different_dates=1,
                max_recurrent_mutations=-1,
            )
            retro_groups = ts.metadata["sc2ts"]["retro_groups"]
            assert len(retro_groups) == 0
            assert "Skipping num_recurrent_mutations=0 exceeds threshold" in caplog.text

    def test_2020_02_14_skip_max_mutations(
        self,
        tmp_path,
        fx_ts_map,
        fx_alignment_store,
        fx_metadata_db,
        fx_match_db,
        caplog,
    ):
        date = "2020-02-14"
        assert len(list(fx_metadata_db.get(date))) == 0
        with caplog.at_level("DEBUG", logger="sc2ts.inference"):
            ts = sc2ts.extend(
                alignment_store=fx_alignment_store,
                metadata_db=fx_metadata_db,
                base_ts=fx_ts_map["2020-02-13"],
                date="2020-02-15",
                match_db=fx_match_db,
                min_root_mutations=0,
                min_group_size=1,
                min_different_dates=1,
                max_recurrent_mutations=100,
                # This should allow everything in but exclude on max_mtuations
                max_mutations_per_sample=-1,
            )
            retro_groups = ts.metadata["sc2ts"]["retro_groups"]
            assert len(retro_groups) == 0
            assert (
                "Skipping mean_mutations_per_sample=1.0 exceeds threshold"
                in caplog.text
            )

    def test_2020_02_14_skip_root_mutations(
        self,
        tmp_path,
        fx_ts_map,
        fx_alignment_store,
        fx_metadata_db,
        fx_match_db,
        caplog,
    ):
        date = "2020-02-14"
        assert len(list(fx_metadata_db.get(date))) == 0
        with caplog.at_level("DEBUG", logger="sc2ts.inference"):
            ts = sc2ts.extend(
                alignment_store=fx_alignment_store,
                metadata_db=fx_metadata_db,
                base_ts=fx_ts_map["2020-02-13"],
                date="2020-02-15",
                match_db=fx_match_db,
                # This should allow everything in but exclude on min_root_mutations
                min_root_mutations=100,
                min_group_size=1,
                min_different_dates=1,
            )
            retro_groups = ts.metadata["sc2ts"]["retro_groups"]
            assert len(retro_groups) == 0
            assert "Skipping root_mutations=0 < threshold" in caplog.text

    def test_2020_02_14_skip_group_size(
        self,
        tmp_path,
        fx_ts_map,
        fx_alignment_store,
        fx_metadata_db,
        fx_match_db,
        caplog,
    ):
        date = "2020-02-14"
        assert len(list(fx_metadata_db.get(date))) == 0
        with caplog.at_level("DEBUG", logger="sc2ts.inference"):
            ts = sc2ts.extend(
                alignment_store=fx_alignment_store,
                metadata_db=fx_metadata_db,
                base_ts=fx_ts_map["2020-02-13"],
                date="2020-02-15",
                match_db=fx_match_db,
                min_root_mutations=0,
                # This should allow everything in but exclude on group size
                min_group_size=100,
                min_different_dates=1,
            )
            retro_groups = ts.metadata["sc2ts"]["retro_groups"]
            assert len(retro_groups) == 0
            assert "Skipping size=" in caplog.text

    @pytest.mark.parametrize("date", dates)
    def test_date_metadata(self, fx_ts_map, date):
        ts = fx_ts_map[date]
        assert ts.metadata["sc2ts"]["date"] == date
        samples_strain = [ts.node(u).metadata["strain"] for u in ts.samples()]
        assert ts.metadata["sc2ts"]["samples_strain"] == samples_strain
        # print(ts.tables.mutations)
        # print(ts.draw_text())

    @pytest.mark.parametrize("date", dates)
    def test_date_validate(self, fx_ts_map, fx_alignment_store, date):
        ts = fx_ts_map[date]
        sc2ts.validate(ts, fx_alignment_store)

    def test_mutation_type_metadata(self, fx_ts_map):
        ts = fx_ts_map[self.dates[-1]]
        for mutation in ts.mutations():
            md = mutation.metadata["sc2ts"]
            assert md["type"] in ["parsimony", "overlap"]

    def test_node_type_metadata(self, fx_ts_map):
        ts = fx_ts_map[self.dates[-1]]
        for node in list(ts.nodes())[2:]:
            md = node.metadata["sc2ts"]
            if node.is_sample():
                # All samples are added as part of a group
                assert "hmm_match" in md
                assert "group_id" in md

    def test_exact_match_count(self, fx_ts_map):
        ts = fx_ts_map[self.dates[-1]]
        # exact_matches = 0
        md = ts.metadata["sc2ts"]["exact_matches"]
        nodes_num_exact_matches = md["node"]
        by_date = md["date"]
        by_pango = md["pango"]
        total = sum(nodes_num_exact_matches.values())
        assert total == sum(by_pango.values())
        assert total == sum(by_date.values())
        assert total == 8

    @pytest.mark.parametrize(
        ["strain", "num_deletions"],
        [
            ("SRR11597190", 3),
            ("SRR11597164", 1),
            ("SRR11597218", 3),
        ],
    )
    def test_deletion_samples(self, fx_ts_map, strain, num_deletions):
        ts = fx_ts_map[self.dates[-1]]
        u = ts.samples()[ts.metadata["sc2ts"]["samples_strain"].index(strain)]
        md = ts.node(u).metadata["sc2ts"]
        assert md["alignment_composition"]["-"] == num_deletions

    @pytest.mark.parametrize("position", [1547, 3951, 3952, 3953, 29749, 29750, 29751])
    def test_deletion_tracking(self, fx_ts_map, position):
        ts = fx_ts_map[self.dates[-1]]
        site = ts.site(position=position)
        assert site.metadata["sc2ts"]["deletion_samples"] == 1

    @pytest.mark.parametrize(
        ["gid", "date", "internal", "strains"],
        [
            (
                "02984ed831cd3c72d206959449dcf8c9",
                "2020-01-19",
                0,
                ["SRR11772659"],
            ),
            (
                "635b05f53af60d8385226cd0e00e97ab",
                "2020-02-08",
                0,
                ["SRR11597163"],
            ),
            (
                "0c36395a702379413ffc855f847873c6",
                "2020-01-24",
                1,
                ["SRR11397727", "SRR11397730"],
            ),
            (
                "9d00e2a016661caea4c2d9abf83375b8",
                "2020-01-30",
                1,
                ["SRR12162232", "SRR12162233", "SRR12162234", "SRR12162235"],
            ),
        ],
    )
    def test_group(self, fx_ts_map, gid, date, internal, strains):
        ts = fx_ts_map[self.dates[-1]]
        samples = []
        num_internal = 0
        got_strains = []
        for node in ts.nodes():
            md = node.metadata
            group = md["sc2ts"].get("group_id", None)
            if group == gid:
                assert node.flags & sc2ts.NODE_IN_SAMPLE_GROUP > 0
                if node.is_sample():
                    got_strains.append(md["strain"])
                    assert md["date"] == date
                else:
                    assert md["sc2ts"]["date_added"] == date
                    num_internal += 1
        assert num_internal == internal
        assert got_strains == strains

    @pytest.mark.parametrize("date", dates[1:])
    def test_node_mutation_counts(self, fx_ts_map, date):
        # Basic check to make sure our fixtures are what we expect.
        # NOTE: this is somewhat fragile as the numbers of nodes does change
        # a little depending on the exact solution that the HMM choses, for
        # example when there are multiple single-mutation matches at different
        # sites.
        ts = fx_ts_map[date]
        expected = {
            "2020-01-19": {"nodes": 3, "mutations": 3},
            "2020-01-24": {"nodes": 6, "mutations": 4},
            "2020-01-25": {"nodes": 8, "mutations": 6},
            "2020-01-28": {"nodes": 10, "mutations": 11},
            "2020-01-29": {"nodes": 12, "mutations": 15},
            "2020-01-30": {"nodes": 17, "mutations": 19},
            "2020-01-31": {"nodes": 18, "mutations": 21},
            "2020-02-01": {"nodes": 23, "mutations": 27},
            "2020-02-02": {"nodes": 28, "mutations": 39},
            "2020-02-03": {"nodes": 31, "mutations": 45},
            "2020-02-04": {"nodes": 35, "mutations": 50},
            "2020-02-05": {"nodes": 35, "mutations": 50},
            "2020-02-06": {"nodes": 40, "mutations": 54},
            "2020-02-07": {"nodes": 42, "mutations": 60},
            "2020-02-08": {"nodes": 47, "mutations": 61},
            "2020-02-09": {"nodes": 48, "mutations": 65},
            "2020-02-10": {"nodes": 49, "mutations": 69},
            "2020-02-11": {"nodes": 50, "mutations": 73},
            "2020-02-13": {"nodes": 53, "mutations": 76},
        }
        assert ts.num_nodes == expected[date]["nodes"]
        assert ts.num_mutations == expected[date]["mutations"]

    @pytest.mark.parametrize(
        ["strain", "parent"],
        [
            ("SRR11397726", 5),
            ("SRR11397729", 5),
            ("SRR11597132", 7),
            ("SRR11597177", 7),
            ("SRR11597156", 7),
        ],
    )
    def test_exact_matches(self, fx_ts_map, strain, parent):
        ts = fx_ts_map[self.dates[-1]]
        md = ts.metadata["sc2ts"]
        assert strain not in md["samples_strain"]
        assert md["exact_matches"]["node"][str(parent)] >= 1


class TestSyntheticAlignments:

    def test_exact_match(self, tmp_path, fx_ts_map, fx_alignment_store):
        # Pick two unique strains and we should match exactly with them
        strains = ["SRR11597218", "ERR4204459"]
        fake_strains = ["fake" + s for s in strains]
        alignments = {
            name: fx_alignment_store[s] for name, s in zip(fake_strains, strains)
        }
        local_as = tmp_alignment_store(tmp_path, alignments)
        date = "2020-03-01"
        metadata_db = tmp_metadata_db(tmp_path, fake_strains, date)

        base_ts = fx_ts_map["2020-02-13"]
        ts = sc2ts.extend(
            alignment_store=local_as,
            metadata_db=metadata_db,
            base_ts=base_ts,
            date=date,
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )
        assert ts.num_nodes == base_ts.num_nodes

        assert (
            sum(ts.metadata["sc2ts"]["exact_matches"]["pango"].values())
            == sum(base_ts.metadata["sc2ts"]["exact_matches"]["pango"].values()) + 2
        )
        samples = ts.samples()
        samples_strain = ts.metadata["sc2ts"]["samples_strain"]
        node_count = ts.metadata["sc2ts"]["exact_matches"]["node"]
        for strain, fake_strain in zip(strains, fake_strains):
            node = samples[samples_strain.index(strain)]
            assert node_count[str(node)] == 1

    def test_recombinant_example_1(self, fx_ts_map, fx_recombinant_example_1):
        base_ts = fx_ts_map["2020-02-13"]
        date = "2020-02-15"
        ts = fx_recombinant_example_1

        assert ts.num_nodes == base_ts.num_nodes + 3
        assert ts.num_edges == base_ts.num_edges + 4
        assert ts.num_samples == base_ts.num_samples + 2
        assert ts.num_mutations == base_ts.num_mutations + 1
        assert ts.num_trees == 2
        samples_strain = ts.metadata["sc2ts"]["samples_strain"]
        assert samples_strain[-2:] == [
            "recombinant_example_1_0",
            "recombinant_example_1_1",
        ]

        group_id = "fc5a70591c67c3db84319c811fec2835"

        left_parent = 31
        right_parent = 46
        bp = 11083

        sample = ts.node(ts.samples()[-2])
        smd = sample.metadata["sc2ts"]
        assert smd["group_id"] == group_id
        assert smd["hmm_match"] == {
            "mutations": [],
            "path": [
                {"left": 0, "parent": left_parent, "right": bp},
                {"left": bp, "parent": right_parent, "right": 29904},
            ],
        }
        assert smd["hmm_reruns"] == {}

        sample = ts.node(ts.samples()[-1])
        smd = sample.metadata["sc2ts"]
        assert smd["group_id"] == group_id
        assert smd["hmm_match"] == {
            "mutations": [
                {"derived_state": "C", "inherited_state": "A", "site_position": 9900}
            ],
            "path": [
                {"left": 0, "parent": left_parent, "right": bp},
                {"left": bp, "parent": right_parent, "right": 29904},
            ],
        }
        assert smd["hmm_reruns"] == {}

        recomb_node = ts.node(ts.num_nodes - 1)
        assert recomb_node.flags == sc2ts.NODE_IS_RECOMBINANT
        smd = recomb_node.metadata["sc2ts"]
        assert smd["date_added"] == date
        assert smd["group_id"] == group_id

        edges = ts.tables.edges[ts.edges_child == recomb_node.id]
        assert len(edges) == 2
        assert edges[0].left == 0
        assert edges[0].right == bp
        assert edges[0].parent == left_parent
        assert edges[1].left == bp
        assert edges[1].right == 29904
        assert edges[1].parent == right_parent

        edges = ts.tables.edges[ts.edges_parent == recomb_node.id]
        assert len(edges) == 2
        assert edges[0].left == 0
        assert edges[0].right == 29904
        assert edges[0].child == ts.samples()[-2]
        assert edges[1].left == 0
        assert edges[1].right == 29904
        assert edges[1].child == ts.samples()[-1]

        ti = sc2ts.TreeInfo(ts, show_progress=False)
        df = ti.recombinants_summary()
        assert df.shape[0] == 1
        row = df.iloc[0]
        assert row.recombinant == recomb_node.id
        assert row.group_id == group_id
        assert row.date_added == date
        assert row.descendants == 2
        assert row.parents == 2
        assert row.causal_pango == {"Unknown": 2}

    def test_recombinant_example_2(self, fx_ts_map, fx_recombinant_example_2):
        base_ts = fx_ts_map["2020-02-13"]
        date = "2020-03-01"
        rts = fx_recombinant_example_2
        samples_strain = rts.metadata["sc2ts"]["samples_strain"]
        assert samples_strain[-3:] == ["left", "right", "recombinant"]

        sample = rts.node(rts.samples()[-1])
        smd = sample.metadata["sc2ts"]
        assert smd["hmm_match"] == {
            "mutations": [],
            "path": [
                {"left": 0, "parent": 53, "right": 29825},
                {"left": 29825, "parent": 54, "right": 29904},
            ],
        }

        assert smd["hmm_reruns"] == {}

    def test_all_As(self, tmp_path, fx_ts_map, fx_alignment_store):
        # Same as the recombinant_example_1() function above
        # Just to get something that looks like an alignment easily
        a = fx_alignment_store["SRR11597188"]
        a[1:] = "A"
        alignments = {"crazytype": a}
        local_as = tmp_alignment_store(tmp_path, alignments)
        date = "2020-03-01"
        metadata_db = tmp_metadata_db(tmp_path, list(alignments.keys()), date)

        base_ts = fx_ts_map["2020-02-13"]
        ts = sc2ts.extend(
            alignment_store=local_as,
            metadata_db=metadata_db,
            base_ts=base_ts,
            date=date,
            match_db=sc2ts.MatchDb.initialise(tmp_path / "match.db"),
        )
        # Super high HMM cost means we don't add it in.
        assert ts.num_nodes == base_ts.num_nodes


class TestMatchingDetails:
    @pytest.mark.parametrize(
        ("strain", "parent"), [("SRR11597207", 34), ("ERR4205570", 47)]
    )
    @pytest.mark.parametrize("num_mismatches", [2, 3, 4])
    def test_exact_matches(
        self,
        fx_ts_map,
        fx_alignment_store,
        strain,
        parent,
        num_mismatches,
    ):
        ts = fx_ts_map["2020-02-10"]
        samples = sc2ts.preprocess(
            [strain],
            fx_alignment_store.path,
            keep_sites=ts.sites_position.astype(int),
        )
        mu, rho = sc2ts.solve_num_mismatches(num_mismatches)
        matches = sc2ts.match_tsinfer(
            samples=samples,
            ts=ts,
            mu=mu,
            rho=rho,
            likelihood_threshold=mu**num_mismatches - 1e-12,
            num_threads=0,
        )
        s = matches[0]
        assert len(s.mutations) == 0
        assert len(s.path) == 1
        assert s.path[0].parent == parent

    @pytest.mark.parametrize(
        ("strain", "parent", "position", "derived_state"),
        [
            ("ERR4206593", 47, 26994, "T"),
        ],
    )
    @pytest.mark.parametrize("num_mismatches", [2, 3, 4])
    def test_one_mismatch(
        self,
        fx_ts_map,
        fx_alignment_store,
        strain,
        parent,
        position,
        derived_state,
        num_mismatches,
    ):
        ts = fx_ts_map["2020-02-10"]
        samples = sc2ts.preprocess(
            [strain],
            fx_alignment_store.path,
            keep_sites=ts.sites_position.astype(int),
        )
        mu, rho = sc2ts.solve_num_mismatches(num_mismatches)
        matches = sc2ts.match_tsinfer(
            samples=samples,
            ts=ts,
            mu=mu,
            rho=rho,
            likelihood_threshold=mu - 1e-5,
            num_threads=0,
        )
        s = matches[0]
        assert len(s.mutations) == 1
        assert s.mutations[0].site_position == position
        assert s.mutations[0].derived_state == derived_state
        assert len(s.path) == 1
        assert s.path[0].parent == parent

    @pytest.mark.parametrize("num_mismatches", [2, 3, 4])
    def test_two_mismatches(
        self,
        fx_ts_map,
        fx_alignment_store,
        num_mismatches,
    ):
        strain = "SRR11597164"
        ts = fx_ts_map["2020-02-01"]
        samples = sc2ts.preprocess(
            [strain],
            fx_alignment_store.path,
            keep_sites=ts.sites_position.astype(int),
        )
        mu, rho = sc2ts.solve_num_mismatches(num_mismatches)
        matches = sc2ts.match_tsinfer(
            samples=samples,
            ts=ts,
            mu=mu,
            rho=rho,
            likelihood_threshold=mu**2 - 1e-12,
            num_threads=0,
        )
        s = matches[0]
        assert len(s.path) == 1
        assert s.path[0].parent == 1
        assert len(s.mutations) == 2

    def test_match_recombinant(self, fx_ts_map):
        ts, s = recombinant_example_1(fx_ts_map)

        mu, rho = sc2ts.solve_num_mismatches(2)
        matches = sc2ts.match_tsinfer(
            samples=[s],
            ts=ts,
            mu=mu,
            rho=rho,
            num_threads=0,
        )
        interval_right = 11083
        left_parent = 31
        right_parent = 46

        m = matches[0]
        assert len(m.mutations) == 0
        assert len(m.path) == 2
        assert m.path[0].parent == left_parent
        assert m.path[0].left == 0
        assert m.path[0].right == interval_right
        assert m.path[1].parent == right_parent
        assert m.path[1].left == interval_right
        assert m.path[1].right == ts.sequence_length


class TestMatchRecombinants:
    def test_example_1(self, fx_ts_map):
        ts, s = recombinant_example_1(fx_ts_map)

        sc2ts.match_recombinants(
            samples=[s],
            base_ts=ts,
            num_mismatches=2,
            num_threads=0,
        )
        left_parent = 31
        right_parent = 46
        interval_right = 11083

        m = s.hmm_reruns["forward"]
        assert len(m.mutations) == 0
        assert len(m.path) == 2
        assert m.path[0].parent == left_parent
        assert m.path[0].left == 0
        assert m.path[0].right == interval_right
        assert m.path[1].parent == right_parent
        assert m.path[1].left == interval_right
        assert m.path[1].right == ts.sequence_length

        interval_left = 3788
        m = s.hmm_reruns["reverse"]
        assert len(m.mutations) == 0
        assert len(m.path) == 2
        assert m.path[0].parent == left_parent
        assert m.path[0].left == 0
        assert m.path[0].right == interval_left
        assert m.path[1].parent == right_parent
        assert m.path[1].left == interval_left
        assert m.path[1].right == ts.sequence_length

        m = s.hmm_reruns["no_recombination"]
        assert len(m.mutations) == 3
        assert m.mutation_summary() == "[11083T>G, 15324C>T, 29303C>T]"
        assert len(m.path) == 1
        assert m.path[0].parent == left_parent
        assert m.path[0].left == 0
        assert m.path[0].right == ts.sequence_length

        assert "no_recombination" in s.summary()

    def test_all_As(self, fx_ts_map):
        ts = fx_ts_map["2020-02-13"]
        h = np.zeros(ts.num_sites, dtype=np.int8)
        s = sc2ts.Sample("zerotype", "2020-02-14", haplotype=h)

        sc2ts.match_recombinants(
            samples=[s],
            base_ts=ts,
            num_mismatches=3,
            num_threads=0,
        )
        assert len(s.hmm_reruns) == 3
        num_mutations = []
        for hmm_match in s.hmm_reruns.values():
            assert len(hmm_match.path) == 1
            assert len(hmm_match.mutations) == 20943
