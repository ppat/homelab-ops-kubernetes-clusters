#!/usr/bin/env python3
import argparse
import hashlib
import io
import json
import os
import random
import sys

import boto3
from botocore.config import Config

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))
import derive as derivelib  # noqa: E402

BLOCK_SIZE = 2 * 1024 * 1024
WAL_SEGMENT_SIZE = 16 * 1024 * 1024
MPU_PART = 5 * 1024 * 1024


def client(endpoint, access, secret, region):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def lz4_compress(data):
    import lz4.frame

    return lz4.frame.compress(data)


def make_longhorn(s3, bucket, root_prefix, rng, volumes=3, backups_per_volume=3):
    base = "{}backupstore/".format(root_prefix)
    written = {"live_blocks": 0, "orphan_blocks": 0, "cfgs": 0}
    plan = {"volumes": {}}

    for v in range(volumes):
        volume = "pvc-fixture-{:04d}".format(v)
        h = derivelib.longhorn_checksum(volume.encode())
        vprefix = "{}volumes/{}/{}/{}/".format(base, h[0:2], h[2:4], volume)

        pool = []
        for _ in range(30):
            plain = rng.randbytes(BLOCK_SIZE)
            checksum = hashlib.sha512(plain).hexdigest()[:64]
            key = derivelib.block_key(vprefix, checksum)
            s3.put_object(Bucket=bucket, Key=key, Body=lz4_compress(plain))
            pool.append(checksum)
            written["live_blocks"] += 1

        backup_ids = []
        for b in range(backups_per_volume):
            refs = pool[: 20 + b * 5]
            backup_id = "fixture-{:04d}-{}".format(v, b)
            cfg = {
                "Name": "backup-{}".format(backup_id),
                "VolumeName": volume,
                "SnapshotName": "snap-{}".format(backup_id),
                "SnapshotCreatedAt": "2026-09-01T00:0{}:00Z".format(b),
                "CreatedTime": "2026-09-01T00:0{}:10Z".format(b),
                "Size": str(len(refs) * BLOCK_SIZE),
                "Labels": {},
                "Parameters": {"VolumeBlockSize": str(BLOCK_SIZE)},
                "IsIncremental": b > 0,
                "CompressionMethod": "lz4",
                "NewlyUploadedDataSize": "0",
                "ReUploadedDataSize": "0",
                "Blocks": [
                    {"Offset": i * BLOCK_SIZE, "BlockChecksum": c}
                    for i, c in enumerate(refs)
                ],
            }
            key = "{}backups/backup_{}.cfg".format(vprefix, backup_id)
            s3.put_object(
                Bucket=bucket, Key=key, Body=json.dumps(cfg).encode()
            )
            backup_ids.append(backup_id)
            written["cfgs"] += 1

        volume_cfg = {
            "Name": volume,
            "Size": str(30 * BLOCK_SIZE),
            "Labels": {},
            "CreatedTime": "2026-08-01T00:00:00Z",
            "LastBackupName": "backup-{}".format(backup_ids[-1]),
            "LastBackupAt": "2026-09-01T00:02:10Z",
            "BlockCount": "30",
            "BackingImageName": "",
            "BackingImageChecksum": "",
            "CompressionMethod": "lz4",
            "StorageClassName": "longhorn",
            "DataEngine": "v1",
        }
        s3.put_object(
            Bucket=bucket,
            Key="{}volume.cfg".format(vprefix),
            Body=json.dumps(volume_cfg).encode(),
        )
        written["cfgs"] += 1

        orphans = []
        for _ in range(4):
            plain = rng.randbytes(BLOCK_SIZE)
            checksum = hashlib.sha512(plain).hexdigest()[:64]
            key = derivelib.block_key(vprefix, checksum)
            s3.put_object(Bucket=bucket, Key=key, Body=lz4_compress(plain))
            orphans.append(key)
            written["orphan_blocks"] += 1

        s3.put_object(
            Bucket=bucket,
            Key="{}locks/lock-fixture{}.lck".format(vprefix, v),
            Body=json.dumps(
                {"Name": "lock-fixture{}".format(v), "Type": 1, "Acquired": True}
            ).encode(),
        )

        plan["volumes"][volume] = {
            "prefix": vprefix,
            "pool": pool,
            "orphans": orphans,
            "backups": backup_ids,
        }

    return plan, written


BACKUP_INFO_FIELDS = [
    "backup_id", "backup_label", "backup_name", "begin_offset", "begin_time",
    "begin_wal", "begin_xlog", "children_backup_ids", "cluster_size", "compression",
    "config_file", "copy_stats", "data_checksums", "deduplicated_size", "encryption",
    "end_offset", "end_time", "end_wal", "end_xlog", "error", "hba_file", "ident_file",
    "included_files", "mode", "parent_backup_id", "pgdata", "server_name",
    "snapshots_info", "status", "summarize_wal", "systemid", "tablespaces", "timeline",
    "version", "xlog_segment_size", "size",
]


def backup_info_body(values):
    out = io.StringIO()
    for name in sorted(BACKUP_INFO_FIELDS):
        out.write("{}={}\n".format(name, values.get(name, "None")))
    return out.getvalue().encode("utf-8")


def wal_name(timeline, logid, segno):
    return "{:08X}{:08X}{:08X}".format(timeline, logid, segno)


def make_barman(s3, bucket, rng, servers, base_backup_parts, wal_gap_in=None):
    plan = {"servers": {}}
    for db, server, first_seg, n_segs in servers:
        prefix = "{}/{}".format(db, server)
        backup_id = "20260901T000000"
        begin = wal_name(1, 0, first_seg)
        end = wal_name(1, 0, first_seg + 1)
        s3.put_object(
            Bucket=bucket,
            Key="{}/base/{}/backup.info".format(prefix, backup_id),
            Body=backup_info_body(
                {
                    "backup_id": backup_id,
                    "server_name": server,
                    "status": "DONE",
                    "timeline": "1",
                    "begin_wal": begin,
                    "end_wal": end,
                    "begin_xlog": "0/{:X}000000".format(first_seg),
                    "end_xlog": "0/{:X}000000".format(first_seg + 1),
                    "begin_time": "Tue Sep  1 00:00:00 2026",
                    "end_time": "Tue Sep  1 00:05:00 2026",
                    "compression": "snappy",
                    "mode": "cloud-backup",
                    "version": "160004",
                    "xlog_segment_size": str(WAL_SEGMENT_SIZE),
                    "size": str(12 * 1024 * 1024),
                    "systemid": "7000000000000000000",
                }
            ),
        )

        data_key = "{}/base/{}/data.tar.snappy".format(prefix, backup_id)
        body = rng.randbytes(MPU_PART * base_backup_parts + 1024)
        mpu = s3.create_multipart_upload(Bucket=bucket, Key=data_key)
        parts = []
        for i in range(0, len(body), MPU_PART):
            chunk = body[i : i + MPU_PART]
            n = len(parts) + 1
            r = s3.upload_part(
                Bucket=bucket,
                Key=data_key,
                UploadId=mpu["UploadId"],
                PartNumber=n,
                Body=chunk,
            )
            parts.append({"ETag": r["ETag"], "PartNumber": n})
        s3.complete_multipart_upload(
            Bucket=bucket,
            Key=data_key,
            UploadId=mpu["UploadId"],
            MultipartUpload={"Parts": parts},
        )

        segs = []
        for i in range(n_segs):
            seg = first_seg + i
            if wal_gap_in == server and i == n_segs // 2:
                continue
            name = wal_name(1, 0, seg)
            key = "{}/wals/{}/{}.snappy".format(prefix, name[:16], name)
            s3.put_object(Bucket=bucket, Key=key, Body=rng.randbytes(WAL_SEGMENT_SIZE))
            segs.append(name)
        plan["servers"][prefix] = {"backup_id": backup_id, "segments": segs}
    return plan


def make_delete_markers(s3, bucket, keys):
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    for key in keys:
        s3.put_object(Bucket=bucket, Key=key, Body=b"tombstoned")
    s3.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Suspended"}
    )
    for key in keys:
        s3.delete_object(Bucket=bucket, Key=key)


def producer_tick(s3, args, fixture, rng):
    added = []
    volume = sorted(fixture["longhorn"]["volumes"])[0]
    vinfo = fixture["longhorn"]["volumes"][volume]
    new_checksums = []
    for _ in range(3):
        plain = rng.randbytes(BLOCK_SIZE)
        checksum = hashlib.sha512(plain).hexdigest()[:64]
        key = derivelib.block_key(vinfo["prefix"], checksum)
        s3.put_object(Bucket=args.longhorn_bucket, Key=key, Body=lz4_compress(plain))
        new_checksums.append(checksum)
        added.append(key)
    refs = vinfo["pool"][:25] + new_checksums
    backup_id = "fixture-tick-{}".format(rng.randrange(1 << 24))
    cfg_key = "{}backups/backup_{}.cfg".format(vinfo["prefix"], backup_id)
    s3.put_object(
        Bucket=args.longhorn_bucket,
        Key=cfg_key,
        Body=json.dumps(
            {
                "Name": "backup-{}".format(backup_id),
                "VolumeName": volume,
                "SnapshotName": "snap-{}".format(backup_id),
                "CreatedTime": "2026-09-02T00:00:00Z",
                "Size": str(len(refs) * BLOCK_SIZE),
                "Labels": {},
                "Parameters": {"VolumeBlockSize": str(BLOCK_SIZE)},
                "IsIncremental": True,
                "CompressionMethod": "lz4",
                "NewlyUploadedDataSize": "0",
                "ReUploadedDataSize": "0",
                "Blocks": [
                    {"Offset": i * BLOCK_SIZE, "BlockChecksum": c}
                    for i, c in enumerate(refs)
                ],
            }
        ).encode(),
    )
    added.append(cfg_key)

    for server, sinfo in fixture["cnpg"]["servers"].items():
        last = sinfo["segments"][-1]
        timeline, logid, segno = (
            int(last[0:8], 16),
            int(last[8:16], 16),
            int(last[16:24], 16),
        )
        for i in (1, 2):
            name = wal_name(timeline, logid, segno + i)
            key = "{}/wals/{}/{}.snappy".format(server, name[:16], name)
            s3.put_object(
                Bucket=args.cnpg_bucket, Key=key, Body=rng.randbytes(WAL_SEGMENT_SIZE)
            )
            added.append(key)
            sinfo["segments"].append(name)
    return added


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", required=True)
    p.add_argument("--access-key", required=True)
    p.add_argument("--secret-key", required=True)
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--longhorn-bucket", default="nas-longhorn-backups")
    p.add_argument("--cnpg-bucket", default="nas-cloudnativepg-backups")
    p.add_argument("--root-prefix", default="cluster-homelab/")
    p.add_argument("--seed", type=int, default=20260903)
    p.add_argument(
        "--base-backup-parts",
        type=int,
        default=15,
        help="5 MiB parts per base backup; 13+ puts it above migrate.SINGLE_PUT_MAX so "
        "the destination write is multipart, as a real base backup's is",
    )
    p.add_argument("--out", default="/state/fixture.json")
    p.add_argument(
        "--tick",
        action="store_true",
        help="do not rebuild; append what the producers would have written since the "
        "bulk copy, and rewrite the fixture manifest. Rehearses the delta copy.",
    )
    args = p.parse_args()

    rng = random.Random(args.seed)
    s3 = client(args.endpoint, args.access_key, args.secret_key, args.region)
    if args.tick:
        with open(args.out, encoding="utf-8") as fh:
            fixture = json.load(fh)
        rng = random.Random()
        added = producer_tick(s3, args, fixture, rng)
        fixture["last_tick"] = added
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(fixture, fh, indent=2, sort_keys=True)
        print("\n".join(added))
        print("{} objects written by the simulated producer tick".format(len(added)))
        return
    for bucket in (args.longhorn_bucket, args.cnpg_bucket):
        try:
            s3.create_bucket(Bucket=bucket)
        except s3.exceptions.ClientError:
            pass

    lh_plan, counts = make_longhorn(s3, args.longhorn_bucket, args.root_prefix, rng)

    victim_volume = sorted(lh_plan["volumes"])[0]
    victim = lh_plan["volumes"][victim_volume]
    eaten = derivelib.block_key(victim["prefix"], victim["pool"][0])
    make_delete_markers(s3, args.longhorn_bucket, [])
    s3.delete_object(Bucket=args.longhorn_bucket, Key=eaten)
    lh_plan["expiry_eaten_block"] = eaten

    marker_keys = [
        "{}backupstore/volumes/00/00/pvc-deleted-{:03d}/volume.cfg".format(
            args.root_prefix, i
        )
        for i in range(5)
    ]
    make_delete_markers(s3, args.longhorn_bucket, marker_keys)
    lh_plan["delete_marker_keys"] = marker_keys

    cnpg_plan = make_barman(
        s3,
        args.cnpg_bucket,
        rng,
        servers=[
            ("coder", "coder-db-v20260310", 0x10, 6),
            ("coder", "coder-db-v20251017", 0x01, 3),
            ("harbor", "harbor-db-v20251215", 0x20, 6),
        ],
        base_backup_parts=args.base_backup_parts,
        wal_gap_in=None,
    )

    out = {
        "longhorn": lh_plan,
        "cnpg": cnpg_plan,
        "counts": counts,
        "block_size": BLOCK_SIZE,
        "wal_segment_size": WAL_SEGMENT_SIZE,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
    print(json.dumps(counts, indent=2))
    print("expiry-eaten block: {}".format(eaten))
    print("fixture manifest: {}".format(args.out))


if __name__ == "__main__":
    main()
