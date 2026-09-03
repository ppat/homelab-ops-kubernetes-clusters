#!/usr/bin/env python3
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import botocore.exceptions  # noqa: E402
import derive  # noqa: E402
import migrate  # noqa: E402


def wal(timeline, logid, segno):
    return "s/wals/x/{:08X}{:08X}{:08X}".format(timeline, logid, segno)


class WalContinuity(unittest.TestCase):
    def test_contiguous_chain_has_no_gaps(self):
        keys = [wal(1, 0, n) for n in range(1, 6)]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual(a["gaps"], [])
        self.assertEqual(a["timelines"][1]["segments"], 5)

    def test_interior_gap_is_found_and_named(self):
        keys = [wal(1, 0, n) for n in (1, 2, 4, 5)]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual([g["kind"] for g in a["gaps"]], ["INTERIOR_GAP"])
        self.assertEqual(a["gaps"][0]["segment"], "000000010000000000000003")

    def test_a_timeline_starting_mid_log_is_not_a_gap(self):
        keys = [wal(2, 0, n) for n in (0x40, 0x41, 0x42)]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual(a["gaps"], [])

    def test_log_boundary_at_16mib_segments(self):
        keys = [wal(1, 0, 0xFE), wal(1, 0, 0xFF), wal(1, 1, 0x00)]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual(a["gaps"], [])

    def test_missing_history_for_a_promoted_timeline(self):
        keys = [wal(1, 0, 1), wal(2, 0, 2)]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual(a["missing_histories"], ["00000002.history"])

    def test_present_history_satisfies_it(self):
        keys = [wal(1, 0, 1), wal(2, 0, 2), "s/wals/00000002.history"]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual(a["missing_histories"], [])

    def test_backup_span_not_covered_is_reported(self):
        keys = [wal(1, 0, 5)]
        backups = {
            "b1": {
                "begin_wal": "000000010000000000000005",
                "end_wal": "000000010000000000000007",
            }
        }
        a = derive.analyse_wal_chain(keys, backups)
        kinds = sorted({g["kind"] for g in a["gaps"]})
        self.assertEqual(kinds, ["BACKUP_NOT_COVERED"])
        self.assertEqual(len(a["gaps"]), 2)

    def test_compression_suffix_is_stripped(self):
        keys = [wal(1, 0, 1) + ".snappy", wal(1, 0, 2) + ".snappy"]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual(a["timelines"][1]["segments"], 2)
        self.assertEqual(a["unrecognised_objects"], [])

    def test_backup_label_and_partial_are_not_chain_members(self):
        keys = [
            wal(1, 0, 1),
            "s/wals/x/000000010000000000000001.00000028.backup",
            "s/wals/x/000000010000000000000002.partial",
        ]
        a = derive.analyse_wal_chain(keys, {})
        self.assertEqual(a["timelines"][1]["segments"], 1)
        self.assertEqual(a["unrecognised_objects"], [])


class LonghornPaths(unittest.TestCase):
    def test_checksum_is_sha512_truncated_not_sha256(self):
        h = derive.longhorn_checksum(b"pvc-0001")
        self.assertEqual(len(h), 64)
        import hashlib

        self.assertEqual(h, hashlib.sha512(b"pvc-0001").hexdigest()[:64])
        self.assertNotEqual(h, hashlib.sha256(b"pvc-0001").hexdigest())

    def test_block_key_uses_the_two_level_fanout(self):
        c = "ab" + "cd" + "e" * 60
        self.assertEqual(
            derive.block_key("p/", c), "p/blocks/ab/cd/{}.blk".format(c)
        )

    def test_a_volume_filed_under_the_wrong_fanout_is_fatal(self):
        inv = {"backupstore/volumes/00/00/pvc-x/volume.cfg": 10}
        with self.assertRaises(derive.DerivationError):
            derive.derive_longhorn(inv, lambda k: b"{}")


class LonghornClosure(unittest.TestCase):
    def _tree(self, referenced, present):
        volume = "pvc-t"
        h = derive.longhorn_checksum(volume.encode())
        p = "backupstore/volumes/{}/{}/{}/".format(h[0:2], h[2:4], volume)
        inv = {p + "volume.cfg": 1, p + "backups/backup_b1.cfg": 1}
        for c in present:
            inv[derive.block_key(p, c)] = 2048
        bodies = {
            p + "volume.cfg": b'{"Name":"pvc-t","CompressionMethod":"lz4"}',
            p
            + "backups/backup_b1.cfg": (
                '{"Name":"b1","Blocks":['
                + ",".join(
                    '{{"Offset":{},"BlockChecksum":"{}"}}'.format(i * 2048, c)
                    for i, c in enumerate(referenced)
                )
                + "]}"
            ).encode(),
        }
        return inv, bodies.__getitem__, p

    def test_unreferenced_blocks_are_orphans_not_copies(self):
        live_c = "aa" * 32
        orphan_c = "bb" * 32
        inv, get, p = self._tree([live_c], [live_c, orphan_c])
        d = derive.derive_longhorn(inv, get)
        self.assertIn(derive.block_key(p, live_c), d.live)
        self.assertNotIn(derive.block_key(p, orphan_c), d.live)
        self.assertEqual(d.orphans, [derive.block_key(p, orphan_c)])

    def test_a_referenced_but_absent_block_is_a_source_hole(self):
        gone_c = "cc" * 32
        inv, get, p = self._tree([gone_c], [])
        d = derive.derive_longhorn(inv, get)
        self.assertEqual(len(d.holes), 1)
        self.assertEqual(d.holes[0].key, derive.block_key(p, gone_c))
        self.assertEqual(d.live_bytes, 2)

    def test_lock_files_are_excluded_never_copied(self):
        c = "dd" * 32
        inv, get, p = self._tree([c], [c])
        inv[p + "locks/lock-abc.lck"] = 64
        d = derive.derive_longhorn(inv, get)
        self.assertEqual(d.excluded, [p + "locks/lock-abc.lck"])
        self.assertNotIn(p + "locks/lock-abc.lck", d.live)

    def test_a_backup_with_no_blocks_and_no_singlefile_is_fatal(self):
        volume = "pvc-t"
        h = derive.longhorn_checksum(volume.encode())
        p = "backupstore/volumes/{}/{}/{}/".format(h[0:2], h[2:4], volume)
        inv = {p + "volume.cfg": 1, p + "backups/backup_b1.cfg": 1}
        bodies = {p + "volume.cfg": b"{}", p + "backups/backup_b1.cfg": b'{"Name":"b1"}'}
        with self.assertRaises(derive.DerivationError):
            derive.derive_longhorn(inv, bodies.__getitem__)

    def test_an_unreadable_catalogue_is_fatal_not_skipped(self):
        c = "ee" * 32
        inv, get, p = self._tree([c], [c])

        for victim in ("backup_b1.cfg", "volume.cfg"):

            def boom(key, victim=victim):
                if key.endswith(victim):
                    raise OSError("503 from the store")
                return get(key)

            with self.assertRaises(derive.DerivationError) as caught:
                derive.derive_longhorn(inv, boom)
            self.assertIn("could not read catalogue file", str(caught.exception))
            self.assertIn("503 from the store", str(caught.exception))


class VolumeWithNoVolumeCfg(unittest.TestCase):
    """A volume that has lost only its volume.cfg keeps every object it still has.

    volume.cfg is a few hundred bytes of reconstructible JSON; the blocks are not
    reconstructible at all. Longhorn's own manager was logging this exact condition
    against this store through 2026-08-19, so it is a live shape, not a hypothetical.
    """

    def _tree(self, with_volume_cfg):
        volume = "pvc-damaged"
        h = derive.longhorn_checksum(volume.encode())
        p = "backupstore/volumes/{}/{}/{}/".format(h[0:2], h[2:4], volume)
        c = "aa" * 32
        inv = {p + "backups/backup_b1.cfg": 1, derive.block_key(p, c): 2048}
        bodies = {
            p
            + "backups/backup_b1.cfg": (
                '{"Name":"b1","Blocks":[{"Offset":0,"BlockChecksum":"' + c + '"}]}'
            ).encode()
        }
        if with_volume_cfg:
            inv[p + "volume.cfg"] = 1
            bodies[p + "volume.cfg"] = b'{"Name":"pvc-damaged"}'
        return inv, bodies.__getitem__, p, c, volume

    def test_its_backup_set_is_copied_not_dropped(self):
        inv, get, p, c, volume = self._tree(with_volume_cfg=False)
        d = derive.derive_longhorn(inv, get)
        self.assertIn(p + "backups/backup_b1.cfg", d.live)
        self.assertIn(derive.block_key(p, c), d.live)
        self.assertEqual(d.orphans, [])
        self.assertEqual(len(d.live), len(inv))

    def test_it_is_named_as_damaged_so_a_human_has_to_look(self):
        inv, get, p, c, volume = self._tree(with_volume_cfg=False)
        d = derive.derive_longhorn(inv, get)
        self.assertIn(volume, d.damaged)
        self.assertEqual(d.summary()["damaged_volumes"], 1)
        self.assertEqual([h.key for h in d.holes], [p + "volume.cfg"])

    def test_an_intact_volume_is_not_damaged(self):
        inv, get, p, c, volume = self._tree(with_volume_cfg=True)
        d = derive.derive_longhorn(inv, get)
        self.assertEqual(d.damaged, {})
        self.assertEqual(d.holes, [])

    def test_blocks_with_no_catalogue_at_all_stay_orphans_but_are_counted(self):
        volume = "pvc-gone"
        h = derive.longhorn_checksum(volume.encode())
        p = "backupstore/volumes/{}/{}/{}/".format(h[0:2], h[2:4], volume)
        c = "bb" * 32
        inv = {derive.block_key(p, c): 2048}
        d = derive.derive_longhorn(inv, lambda k: b"{}")
        self.assertEqual(d.live, {})
        self.assertEqual(d.orphans, [derive.block_key(p, c)])
        self.assertEqual(
            d.detail["volumes_without_catalogue"][volume],
            {"block_objects": 1, "bytes": 2048},
        )


class ObjectVanishingMidDerivation(unittest.TestCase):
    """Producers write and garbage-collect throughout a multi-hour pass.

    A catalogue file deleted between the listing and the read is the producer's own
    retention, not a store fault, and the two must not be indistinguishable: one is
    ordinary and the other must stop the run.
    """

    def _longhorn(self, vanish):
        volume = "pvc-v"
        h = derive.longhorn_checksum(volume.encode())
        p = "backupstore/volumes/{}/{}/{}/".format(h[0:2], h[2:4], volume)
        c = "cc" * 32
        inv = {
            p + "volume.cfg": 1,
            p + "backups/backup_b1.cfg": 1,
            derive.block_key(p, c): 2048,
        }
        bodies = {
            p + "volume.cfg": b'{"Name":"pvc-v"}',
            p
            + "backups/backup_b1.cfg": (
                '{"Name":"b1","Blocks":[{"Offset":0,"BlockChecksum":"' + c + '"}]}'
            ).encode(),
        }

        def get(key):
            if key.endswith(vanish):
                raise derive.ObjectVanished(key)
            return bodies[key]

        return inv, get, p, c, volume

    def test_a_vanished_backup_cfg_drops_that_backup_and_orphans_its_blocks(self):
        inv, get, p, c, volume = self._longhorn("backup_b1.cfg")
        d = derive.derive_longhorn(inv, get)
        self.assertEqual(d.vanished, [p + "backups/backup_b1.cfg"])
        self.assertNotIn(p + "backups/backup_b1.cfg", d.live)
        self.assertEqual(d.orphans, [derive.block_key(p, c)])
        self.assertEqual(len(d.warnings), 1)

    def test_a_vanished_volume_cfg_is_damage_not_a_dropped_backup_set(self):
        inv, get, p, c, volume = self._longhorn("volume.cfg")
        d = derive.derive_longhorn(inv, get)
        self.assertEqual(d.vanished, [p + "volume.cfg"])
        self.assertIn(volume, d.damaged)
        self.assertIn(derive.block_key(p, c), d.live)

    def test_a_store_error_is_still_fatal(self):
        inv, get, p, c, volume = self._longhorn("nothing-matches-this")

        def boom(key):
            if key.endswith("backup_b1.cfg"):
                raise OSError("503 from the store")
            return get(key)

        with self.assertRaises(derive.DerivationError):
            derive.derive_longhorn(inv, boom)

    def test_a_vanished_backup_info_still_carries_its_members(self):
        inv = {
            "db/s/base/b1/backup.info": 1,
            "db/s/base/b1/data.tar.snappy": 9,
        }

        def get(key):
            raise derive.ObjectVanished(key)

        d = derive.derive_barman(inv, get)
        self.assertEqual(d.vanished, ["db/s/base/b1/backup.info"])
        self.assertEqual(list(d.live), ["db/s/base/b1/data.tar.snappy"])


class BarmanCatalogue(unittest.TestCase):
    def test_backup_info_parses_the_field_list_format(self):
        body = b"status=DONE\nbegin_wal=000000010000000000000001\nerror=None\n"
        info = derive.parse_backup_info("k", body)
        self.assertEqual(info["status"], "DONE")
        self.assertIsNone(info["error"])

    def test_a_malformed_backup_info_is_fatal(self):
        with self.assertRaises(derive.DerivationError):
            derive.parse_backup_info("k", b"this is not a field list")

    def test_every_server_generation_is_kept_by_default(self):
        inv = {
            "db/db-v2/base/20260101T000000/backup.info": 1,
            "db/db-v2/base/20260101T000000/data.tar.snappy": 9,
            "db/db-v1/base/20250101T000000/backup.info": 1,
            "db/db-v1/base/20250101T000000/data.tar.snappy": 9,
        }
        get = {k: b"status=DONE\n" for k in inv}.__getitem__
        d = derive.derive_barman(inv, get)
        self.assertEqual(len(d.live), 4)
        self.assertEqual(sorted(d.detail["servers"]), ["db/db-v1", "db/db-v2"])

    def test_an_operator_can_exclude_a_generation_explicitly(self):
        inv = {
            "db/db-v2/base/20260101T000000/backup.info": 1,
            "db/db-v1/base/20250101T000000/backup.info": 1,
        }
        get = {k: b"status=DONE\n" for k in inv}.__getitem__
        d = derive.derive_barman(inv, get, exclude_servers=["db/db-v1"])
        self.assertEqual(list(d.live), ["db/db-v2/base/20260101T000000/backup.info"])

    def test_backup_members_come_from_the_listing_not_a_name_pattern(self):
        inv = {
            "db/s/base/b1/backup.info": 1,
            "db/s/base/b1/data.tar.snappy": 9,
            "db/s/base/b1/data_0001.tar.snappy": 9,
            "db/s/base/b1/16385.tar.snappy": 9,
        }
        get = {k: b"status=DONE\n" for k in inv}.__getitem__
        d = derive.derive_barman(inv, get)
        self.assertEqual(len(d.live), 4)

    def test_a_backup_info_with_no_data_objects_is_a_source_hole(self):
        inv = {"db/s/base/b1/backup.info": 1}
        get = {k: b"status=DONE\n" for k in inv}.__getitem__
        d = derive.derive_barman(inv, get)
        self.assertEqual(len(d.holes), 1)

    def test_a_key_outside_base_and_wals_is_unclassified(self):
        inv = {"db/s/base/b1/backup.info": 1, "stray-object": 1}
        get = {"db/s/base/b1/backup.info": b"status=DONE\n"}.__getitem__
        d = derive.derive_barman(inv, get)
        self.assertEqual(d.unclassified, ["stray-object"])


class UnclassifiedPromotion(unittest.TestCase):
    """`--allow-unclassified` is the tool's own prescribed escape hatch.

    Its message tells the operator to re-run with it "to copy them as-is". If the flag
    only suppressed the exit code, following that instruction would produce a green run
    that left the objects behind -- in a corpus where a missing object is caught by
    nobody, at any layer, until the day it is needed.
    """

    def test_the_flag_puts_the_keys_in_the_copy_list(self):
        inv = {"db/s/base/b1/backup.info": 1, "some-new-shape/important.tar": 4096}
        get = {"db/s/base/b1/backup.info": b"status=DONE\n"}.__getitem__
        d = derive.derive_barman(inv, get)
        self.assertEqual(d.unclassified, ["some-new-shape/important.tar"])
        self.assertNotIn("some-new-shape/important.tar", d.live)

        migrate.promote_unclassified(d, inv)
        self.assertIn("some-new-shape/important.tar", d.live)
        self.assertEqual(d.live["some-new-shape/important.tar"], 4096)


class SourceClientIsReadOnly(unittest.TestCase):
    """The claim is "structurally cannot damage the store it is reading"."""

    class _Fake:
        def put_object(self, **kwargs):
            raise AssertionError("reached the source")

        def get_object(self, **kwargs):
            return "read"

        def get_paginator(self, name):
            return name

    def test_a_mutation_does_not_reach_the_source(self):
        c = migrate.ReadOnlyClient(self._Fake())
        with self.assertRaises(RuntimeError):
            c.put_object(Bucket="b", Key="k", Body=b"")
        for verb in ("delete_object", "upload_fileobj", "abort_multipart_upload"):
            with self.assertRaises(RuntimeError):
                getattr(c, verb)

    def test_the_reads_the_derivation_needs_still_work(self):
        c = migrate.ReadOnlyClient(self._Fake())
        self.assertEqual(c.get_object(Bucket="b", Key="k"), "read")
        self.assertEqual(c.get_paginator("list_objects_v2"), "list_objects_v2")
        with self.assertRaises(RuntimeError):
            c.get_paginator("list_multipart_uploads")


class ArgumentParsing(unittest.TestCase):
    def test_extra_args_style_multi_flag_invocation_parses(self):
        args = migrate.build_parser().parse_args(
            [
                "verify",
                "--consumer=longhorn",
                "--src-bucket=b",
                "--tier",
                "etag",
                "--tier",
                "bytes",
                "--tier",
                "deep",
                "--use-plan",
            ]
        )
        self.assertEqual(args.tier, ["etag", "bytes", "deep"])
        self.assertTrue(args.use_plan)

    def test_a_usage_error_is_operational_not_a_failed_claim(self):
        self.assertEqual(
            migrate.main(["verify", "--consumer=longhorn"]), migrate.EXIT_OPERATIONAL
        )


class FakeStore:
    """Enough of an S3 client for the copy loop, with a controllable disappearance."""

    def __init__(self, objects=None, vanish=()):
        self.objects = dict(objects or {})
        self.vanish = set(vanish)

    @staticmethod
    def _gone(op):
        return botocore.exceptions.ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "gone"}}, op
        )

    def head_object(self, Bucket, Key):  # noqa: N803 -- boto3's parameter names
        if Key not in self.objects:
            raise self._gone("HeadObject")
        body = self.objects[Key]
        return {
            "ContentLength": len(body),
            "ETag": '"{}"'.format(hashlib.md5(body).hexdigest()),  # noqa: S324
        }

    def get_object(self, Bucket, Key):  # noqa: N803
        if Key in self.vanish or Key not in self.objects:
            raise self._gone("GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, Bucket, Key, Body):  # noqa: N803
        self.objects[Key] = Body
        return {"ETag": '"{}"'.format(hashlib.md5(Body).hexdigest())}  # noqa: S324

    def get_paginator(self, name):
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix=""):  # noqa: N803
                if name == "list_multipart_uploads":
                    return [{}]
                return [
                    {
                        "Contents": [
                            {
                                "Key": k,
                                "Size": len(v),
                                "ETag": '"{}"'.format(hashlib.md5(v).hexdigest()),  # noqa: S324
                            }
                            for k, v in sorted(objects.items())
                            if k.startswith(Prefix)
                        ]
                    }
                ]

        return _P()


class _Args:
    def __init__(self, **kw):
        self.consumer = "cnpg"
        self.src_bucket = "src"
        self.dst_bucket = "dst"
        self.src_prefix = ""
        self.workers = 2
        self.upload_concurrency = 2
        self.large_concurrency = 1
        self.self_check = False
        self.use_plan = True
        self.tier = ["etag"]
        self.tier_bytes_all = False
        self.sample = 0
        self.deep_sample = 0
        self.sample_seed = 1
        self.extra_key = None
        self.exclude_server = None
        self.allow_unclassified = False
        self.allow_damaged_volumes = False
        self.fail_on_source_hole = False
        self.region = "us-east-1"
        self.__dict__.update(kw)


class VanishingDuringCopy(unittest.TestCase):
    """An object deleted between LIST and GET is one line of the summary, not the run.

    Longhorn's retention garbage-collects blocks and barman's deletes WAL continuously,
    and design §15 has both producers writing until their own cutover -- so over a pass
    long enough to matter this is an ordinary event. Before the fix it escaped
    `pool.map`'s result iterator and re-raised after every future had been drained,
    losing the counts, the failure list and the hash manifest for the whole run.
    """

    def _run(self, vanish):
        src = FakeStore({"a": b"aaa", "b": b"bbb", "c": b"ccc"}, vanish=vanish)
        dst = FakeStore()
        state = tempfile.mkdtemp()
        args = _Args(state_dir=state)
        plan = os.path.join(state, "plan-cnpg.jsonl")
        with open(plan, "w", encoding="utf-8") as fh:
            for key, body in sorted(src.objects.items()):
                fh.write(
                    json.dumps(
                        {
                            "key": key,
                            "size": len(body),
                            "etag": hashlib.md5(body).hexdigest(),  # noqa: S324
                        }
                    )
                    + "\n"
                )
        with open(
            os.path.join(state, "derivation-cnpg.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(
                {
                    "summary": {"source_inventory_objects": 3},
                    "detail": {},
                    "catalogue_digests": {},
                    "source_holes": [],
                },
                fh,
            )
        return src, dst, args, state

    def test_the_run_completes_and_counts_it(self):
        src, dst, args, state = self._run(vanish={"b"})
        rc = migrate.cmd_copy_with_clients(src, dst, args)
        self.assertEqual(rc, migrate.EXIT_OK)
        with open(os.path.join(state, "copy-cnpg.json"), encoding="utf-8") as fh:
            summary = json.load(fh)
        self.assertEqual(summary["vanished"], ["b"])
        self.assertEqual(summary["counts"]["COPIED"], 2)
        self.assertEqual(summary["counts"]["FAILED"], 0)
        self.assertEqual(sorted(dst.objects), ["a", "c"])

    def test_verify_passes_against_the_pinned_plan_while_the_source_moves(self):
        src, dst, args, state = self._run(vanish={"b"})
        migrate.cmd_copy_with_clients(src, dst, args)
        # A producer writes to the source after the plan was pinned. A verification
        # that re-derived would see it missing at the destination and report FAIL.
        src.objects["written-after-the-plan"] = b"zzz"
        rc = migrate.cmd_verify_with_clients(src, dst, args)
        self.assertEqual(rc, migrate.EXIT_OK)
        with open(os.path.join(state, "verify-cnpg.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(payload["results"]["population"]["vanished_during_copy"], 1)
        self.assertEqual(payload["results"]["population"]["source_objects_now"], 4)


class WalContinuityIsNotTheCopyVerdict(unittest.TestCase):
    """A gap in MinIO's own WAL chain is a claim about the source.

    `analyse_wal_chain` runs entirely over the source's listing, so a pre-existing gap,
    or a span belonging to a serverName generation whose WAL barman has since aged out,
    would make a byte-perfect copy report FAIL -- and nothing in the estate has ever
    measured whether the chain is intact.
    """

    def test_a_source_gap_does_not_fail_the_copy(self):
        src = FakeStore({"a": b"aaa"})
        dst = FakeStore({"a": b"aaa"})
        state = tempfile.mkdtemp()
        args = _Args(state_dir=state)
        with open(os.path.join(state, "plan-cnpg.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"key": "a", "size": 3, "etag": hashlib.md5(b"aaa").hexdigest()}  # noqa: S324
                )
                + "\n"
            )
        with open(
            os.path.join(state, "derivation-cnpg.json"), "w", encoding="utf-8"
        ) as fh:
            json.dump(
                {
                    "summary": {"source_inventory_objects": 1},
                    "catalogue_digests": {},
                    "source_holes": [],
                    "detail": {
                        "servers": {
                            "db/s": {
                                "wal_analysis": {
                                    "gaps": [
                                        {
                                            "timeline": 1,
                                            "segment": "000000010000000000000003",
                                            "kind": "INTERIOR_GAP",
                                        }
                                    ],
                                    "missing_histories": [],
                                }
                            }
                        }
                    },
                },
                fh,
            )
        rc = migrate.cmd_verify_with_clients(src, dst, args)
        self.assertEqual(rc, migrate.EXIT_OK)
        with open(os.path.join(state, "verify-cnpg.json"), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(payload["verdict"], "PASS")
        self.assertEqual(payload["wal_continuity_verdict"], "SOURCE_INCOMPLETE")


class TransferMemoryIsBounded(unittest.TestCase):
    """s3transfer's defaults are a 10 x 64 MiB in-memory ceiling *per call*.

    boto3 builds a fresh TransferManager, and a fresh semaphore, for every
    `upload_fileobj`, so the defaults scale that ceiling by --workers against a fixed
    container limit. The plan is sorted by key, which puts a barman base backup's
    members in flight together.
    """

    def test_the_in_memory_chunk_ceiling_is_set_not_inherited(self):
        cfg = migrate.transfer_config(_Args())
        self.assertEqual(cfg.max_in_memory_upload_chunks, 2)
        self.assertEqual(cfg.multipart_chunksize, 16 * 1024 * 1024)
        self.assertLessEqual(
            cfg.max_in_memory_upload_chunks * cfg.multipart_chunksize, 64 * 1024 * 1024
        )

    def test_single_put_still_covers_a_wal_segment(self):
        self.assertGreater(migrate.SINGLE_PUT_MAX, derive.DEFAULT_XLOG_SEGMENT_SIZE)


class InjectorRefusesProduction(unittest.TestCase):
    """`inject_failures.py` corrupts objects on purpose, under a path it is handed.

    "Never put test/ in the Job's ConfigMap" protects the cluster and does nothing
    about an operator with a shell on nas and the real LUN mounted. The gateway writes
    `.versitygw-store-identity` at its store root and refuses to start without it, so
    the sentinel's presence is exactly the predicate wanted: a real store always has
    one, a fixture tree never does.
    """

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
        import inject_failures  # noqa: PLC0415

        self.injector = inject_failures
        self.root = tempfile.mkdtemp()

    def test_a_fixture_tree_is_allowed(self):
        bucket = os.path.join(self.root, "nas-longhorn-backups")
        os.makedirs(bucket)
        self.injector.refuse_production_tree(bucket)

    def test_a_store_root_is_refused(self):
        open(
            os.path.join(self.root, self.injector.STORE_IDENTITY_SENTINEL),
            "w",
            encoding="utf-8",
        ).close()
        with self.assertRaises(self.injector.ProductionStore):
            self.injector.refuse_production_tree(self.root)

    def test_a_bucket_below_a_store_root_is_refused(self):
        # --data-root may legitimately name a bucket directory, and the sentinel is at
        # the store root above it. Checking only the given path would miss the one
        # spelling most likely to be typed against production.
        open(
            os.path.join(self.root, self.injector.STORE_IDENTITY_SENTINEL),
            "w",
            encoding="utf-8",
        ).close()
        bucket = os.path.join(self.root, "nas-cloudnativepg-backups")
        os.makedirs(bucket)
        with self.assertRaises(self.injector.ProductionStore):
            self.injector.refuse_production_tree(bucket)


class EtagClassification(unittest.TestCase):
    def test_a_32_hex_etag_is_a_content_hash(self):
        self.assertTrue(migrate.is_single_part_etag('"' + "a" * 32 + '"'))

    def test_a_composite_etag_is_not(self):
        self.assertFalse(migrate.is_single_part_etag('"{}-16"'.format("a" * 32)))

    def test_a_non_hex_etag_is_not(self):
        self.assertFalse(migrate.is_single_part_etag('"' + "z" * 32 + '"'))

    def test_empty_etag_is_not(self):
        self.assertFalse(migrate.is_single_part_etag(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
