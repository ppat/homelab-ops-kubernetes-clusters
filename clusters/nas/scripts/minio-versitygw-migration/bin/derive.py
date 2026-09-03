import hashlib
import json
import re
from dataclasses import dataclass, field


BACKUPSTORE_BASE = "backupstore"
VOLUME_DIRECTORY = "volumes"
BACKUP_DIRECTORY = "backups"
BLOCKS_DIRECTORY = "blocks"
LOCKS_DIRECTORY = "locks"

_VOLUME_CFG_RE = re.compile(r"^volumes/([0-9a-f]{2})/([0-9a-f]{2})/([^/]+)/volume\.cfg$")
_BACKUP_CFG_RE = re.compile(
    r"^volumes/([0-9a-f]{2})/([0-9a-f]{2})/([^/]+)/backups/backup_([^/]+)\.cfg$"
)
_BLOCK_RE = re.compile(
    r"^volumes/([0-9a-f]{2})/([0-9a-f]{2})/([^/]+)/blocks/"
    r"([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{64})\.blk$"
)
_LOCK_RE = re.compile(r"^volumes/([0-9a-f]{2})/([0-9a-f]{2})/([^/]+)/locks/[^/]+\.lck$")


def longhorn_checksum(data):
    return hashlib.sha512(data).hexdigest()[:64]


def block_key(volume_prefix, checksum):
    return "{}blocks/{}/{}/{}.blk".format(
        volume_prefix, checksum[0:2], checksum[2:4], checksum
    )


_BACKUP_INFO_RE = re.compile(r"^(?P<server>.+)/base/(?P<backup_id>[^/]+)/backup\.info$")
_BASE_MEMBER_RE = re.compile(r"^(?P<server>.+)/base/(?P<backup_id>[^/]+)/(?P<rest>.+)$")
_WAL_MEMBER_RE = re.compile(r"^(?P<server>.+)/wals/(?P<rest>.+)$")

_XLOG_RE = re.compile(
    r"^([\dA-Fa-f]{8})"
    r"(?:([\dA-Fa-f]{8})([\dA-Fa-f]{8})(?:\.[\dA-Fa-f]{8}\.backup|\.partial)?"
    r"|\.history)$"
)
_WAL_SEGMENT_RE = re.compile(r"^([\dA-Fa-f]{8})([\dA-Fa-f]{8})([\dA-Fa-f]{8})$")
_HISTORY_RE = re.compile(r"^([\dA-Fa-f]{8})\.history$")

DEFAULT_XLOG_SEGMENT_SIZE = 16 * 1024 * 1024

WAL_COMPRESSION_SUFFIXES = (".snappy", ".gz", ".bz2", ".lz4", ".zst", ".xz")


def strip_wal_suffix(basename):
    for suffix in WAL_COMPRESSION_SUFFIXES:
        if basename.endswith(suffix):
            return basename[: -len(suffix)]
    return basename


HOLE = "SOURCE_HOLE"

ORPHAN = "ORPHAN"

EXCLUDED = "EXCLUDED_BY_RULE"

UNCLASSIFIED = "UNCLASSIFIED"

DAMAGED = "DAMAGED_VOLUME"


@dataclass
class Hole:
    key: str
    referenced_by: str
    detail: str = ""


@dataclass
class Derivation:

    consumer: str
    live: dict = field(default_factory=dict)
    holes: list = field(default_factory=list)
    orphans: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    unclassified: list = field(default_factory=list)
    # Volumes whose own catalogue is incomplete. Their surviving objects are in `live`
    # -- the entry records what is wrong so a human has to look before the copy runs.
    damaged: dict = field(default_factory=dict)
    # Objects the source no longer held by the time they were read. Distinct from a
    # hole: a hole is referenced-but-absent, this is present-at-LIST-then-gone.
    vanished: list = field(default_factory=list)
    # Longhorn's .cfg carries no digest and a dropped Blocks[] entry restores a
    # zero-filled hole that nothing detects. These are that missing digest.
    catalogue_digests: dict = field(default_factory=dict)
    detail: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    @property
    def live_bytes(self):
        return sum(self.live.values())

    def summary(self):
        return {
            "consumer": self.consumer,
            "live_objects": len(self.live),
            "live_bytes": self.live_bytes,
            "source_holes": len(self.holes),
            "orphans": len(self.orphans),
            "excluded": len(self.excluded),
            "unclassified": len(self.unclassified),
            "damaged_volumes": len(self.damaged),
            "vanished_during_derivation": len(self.vanished),
            "catalogue_files": len(self.catalogue_digests),
            "warnings": len(self.warnings),
        }


class DerivationError(Exception):
    """A condition under which continuing would silently produce a wrong copy list."""


class ObjectVanished(Exception):
    """The source no longer holds a key its own listing named moments earlier.

    Raised by the caller-supplied `get_object`, never from here: this module has no
    S3 client and no idea what a 404 looks like. Producers write and garbage-collect
    throughout a multi-hour pass, so this is an ordinary event, not a store fault --
    which is exactly why it must not be indistinguishable from one.
    """


def derive_longhorn(inventory, get_object, root_prefix=""):
    if root_prefix and not root_prefix.endswith("/"):
        root_prefix += "/"
    base = "{}{}/".format(root_prefix, BACKUPSTORE_BASE)

    d = Derivation(consumer="longhorn")

    volume_cfgs = {}
    backup_cfgs = {}
    blocks = {}
    for key in inventory:
        if not key.startswith(base):
            d.unclassified.append(key)
            continue
        rel = key[len(base) :]

        m = _VOLUME_CFG_RE.match(rel)
        if m:
            _assert_volume_fanout(m.group(3), m.group(1), m.group(2), key)
            volume_cfgs[m.group(3)] = key
            continue

        m = _BACKUP_CFG_RE.match(rel)
        if m:
            _assert_volume_fanout(m.group(3), m.group(1), m.group(2), key)
            backup_cfgs.setdefault(m.group(3), {})[m.group(4)] = key
            continue

        m = _BLOCK_RE.match(rel)
        if m:
            _assert_volume_fanout(m.group(3), m.group(1), m.group(2), key)
            checksum = m.group(6)
            if m.group(4) != checksum[0:2] or m.group(5) != checksum[2:4]:
                raise DerivationError(
                    "block at {} is filed under {}/{} but its name hashes to {}/{}; the "
                    "tree does not have the shape Longhorn writes".format(
                        key, m.group(4), m.group(5), checksum[0:2], checksum[2:4]
                    )
                )
            blocks.setdefault(m.group(3), {})[checksum] = key
            continue

        if _LOCK_RE.match(rel):
            d.excluded.append(key)
            continue

        d.unclassified.append(key)

    # A volume is processed whenever it has ANY catalogue file. Its backup_*.cfg files
    # are what resolve the block closure; volume.cfg only carries Size/BlockCount/
    # CompressionMethod. So a volume that has lost only its volume.cfg is still fully
    # derivable, and its blocks -- which are not reconstructible by any means -- get
    # copied. Leaving them behind would make hand-recovery of that volume impossible
    # the day MinIO is retired.
    referenced = {}
    for volume in sorted(set(volume_cfgs) | set(backup_cfgs)):
        cfg_key = volume_cfgs.get(volume)
        volume_cfg = {}
        if cfg_key is None:
            _mark_damaged(
                d,
                volume,
                "volume.cfg is absent; the volume's surviving backups and blocks are "
                "copied and its closure resolved from backup_*.cfg alone",
            )
            d.holes.append(
                Hole(
                    key="{}{}".format(_volume_prefix(base, volume), "volume.cfg"),
                    referenced_by="volume:{}".format(volume),
                    detail="volume directory exists but volume.cfg is absent",
                )
            )
        else:
            body = _fetch_catalogue(get_object, cfg_key)
            if body is None:
                _mark_damaged(
                    d, volume, "volume.cfg vanished from the source mid-derivation"
                )
                d.vanished.append(cfg_key)
            else:
                d.live[cfg_key] = inventory[cfg_key]
                d.catalogue_digests[cfg_key] = hashlib.sha256(body).hexdigest()
                volume_cfg = _parse_json_catalogue(cfg_key, body)
        referenced.setdefault(volume, set())

        vol_detail = {
            "size": volume_cfg.get("Size"),
            "block_count": volume_cfg.get("BlockCount"),
            "compression_method": volume_cfg.get("CompressionMethod"),
            "backups": {},
        }

        for backup_id, backup_key in sorted(backup_cfgs.get(volume, {}).items()):
            backup_body = _fetch_catalogue(get_object, backup_key)
            if backup_body is None:
                # Longhorn's retention deleted this backup while the pass was running.
                # Dropping it is correct: its blocks fall out of `referenced` on their
                # own and are classed as orphans, which is what they now are.
                d.vanished.append(backup_key)
                d.warnings.append(
                    "{} vanished from the source between the listing and the catalogue "
                    "read; dropped from the plan".format(backup_key)
                )
                vol_detail["backups"][backup_id] = {"status": "VANISHED"}
                continue
            d.live[backup_key] = inventory[backup_key]
            d.catalogue_digests[backup_key] = hashlib.sha256(backup_body).hexdigest()
            backup = _parse_json_catalogue(backup_key, backup_body)

            block_list = backup.get("Blocks")
            single_file = backup.get("SingleFile") or {}
            if block_list is None and not single_file:
                raise DerivationError(
                    "{} has neither Blocks[] nor SingleFile; this code does not know "
                    "what objects that backup consists of".format(backup_key)
                )

            checksums = set()
            for entry in block_list or []:
                checksum = entry.get("BlockChecksum")
                if not checksum or not re.fullmatch(r"[0-9a-f]{64}", checksum):
                    raise DerivationError(
                        "{} carries a Blocks[] entry with an unusable BlockChecksum "
                        "{!r}".format(backup_key, checksum)
                    )
                checksums.add(checksum)
            referenced[volume] |= checksums

            for path in _single_file_paths(single_file):
                sf_key = "{}{}".format(base, path.lstrip("/"))
                if sf_key in inventory:
                    d.live[sf_key] = inventory[sf_key]
                else:
                    d.holes.append(
                        Hole(sf_key, backup_key, "SingleFile object absent from source")
                    )

            vol_detail["backups"][backup_id] = {
                "created": backup.get("CreatedTime"),
                "blocks_referenced": len(checksums),
                "incremental": backup.get("IsIncremental"),
                "compression_method": backup.get("CompressionMethod"),
            }

        present = blocks.get(volume, {})
        for checksum in sorted(referenced[volume]):
            key = present.get(checksum)
            if key is None:
                d.holes.append(
                    Hole(
                        key=block_key(_volume_prefix(base, volume), checksum),
                        referenced_by="volume:{}".format(volume),
                        detail="block referenced by a retained backup is absent",
                    )
                )
                continue
            d.live[key] = inventory[key]

        for checksum, key in present.items():
            if checksum not in referenced[volume]:
                d.orphans.append(key)

        vol_detail["blocks_present"] = len(present)
        vol_detail["blocks_referenced"] = len(referenced[volume])
        vol_detail["blocks_orphaned"] = len(present) - len(
            referenced[volume] & set(present)
        )
        d.detail.setdefault("volumes", {})[volume] = vol_detail
        if volume in d.damaged:
            d.damaged[volume]["objects_copied"] = sum(
                1 for k in d.live if k.startswith(_volume_prefix(base, volume))
            )

    # A volume directory holding blocks and no catalogue file at all is what Longhorn
    # leaves mid-garbage-collection after a volume is deleted: nothing references the
    # blocks and nothing ever will again. They stay orphans -- but the count and bytes
    # are reported rather than dissolved into the estate-wide orphan total, because the
    # other reading of the same evidence is "every catalogue file for this volume was
    # destroyed", and only a human can tell those apart.
    for volume in sorted(set(blocks) - set(volume_cfgs) - set(backup_cfgs)):
        keys = sorted(blocks[volume].values())
        d.orphans.extend(keys)
        d.detail.setdefault("volumes_without_catalogue", {})[volume] = {
            "block_objects": len(keys),
            "bytes": sum(inventory[k] for k in keys),
        }

    return d


def _mark_damaged(d, volume, reason):
    d.damaged.setdefault(volume, {"reasons": []})["reasons"].append(reason)


def _volume_prefix(base, volume):
    h = longhorn_checksum(volume.encode("utf-8"))
    return "{}{}/{}/{}/{}/".format(base, VOLUME_DIRECTORY, h[0:2], h[2:4], volume)


def _assert_volume_fanout(volume, layer1, layer2, key):
    h = longhorn_checksum(volume.encode("utf-8"))
    if layer1 != h[0:2] or layer2 != h[2:4]:
        raise DerivationError(
            "volume {!r} is filed under {}/{} at {} but hashes to {}/{}; either the tree "
            "is not Longhorn-written or this code's checksum is wrong".format(
                volume, layer1, layer2, key, h[0:2], h[2:4]
            )
        )


def _single_file_paths(single_file):
    if not single_file:
        return []
    path = single_file.get("FilePath")
    return [path] if path else []


def _fetch_catalogue(get_object, key):
    """Read a catalogue file. `None` means it is legitimately gone, not unreadable.

    The distinction is the whole point: a store that cannot serve a key it just listed
    is a fault that must stop the run, because skipping the file would silently drop
    everything only it references. A key the producer has since deleted is an ordinary
    event over a multi-hour pass and must not.
    """
    try:
        return get_object(key)
    except ObjectVanished:
        return None
    except Exception as exc:  # noqa: BLE001 -- re-raised as a fatal derivation error
        raise DerivationError(
            "could not read catalogue file {}: {}. Refusing to continue: skipping it "
            "would silently drop everything only it references".format(key, exc)
        ) from exc


def _parse_json_catalogue(key, body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise DerivationError(
            "catalogue file {} is not parseable JSON: {}".format(key, exc)
        ) from exc


def parse_backup_info(key, body):
    out = {}
    text = body.decode("utf-8")
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if "=" not in line:
            raise DerivationError(
                "{} line {}: {!r} is not a name=value pair".format(key, lineno, line)
            )
        name, value = line.split("=", 1)
        out[name.strip()] = None if value == "None" else value
    if not out:
        raise DerivationError("{} is empty".format(key))
    return out


def derive_barman(inventory, get_object, exclude_servers=()):
    d = Derivation(consumer="cnpg")
    excluded_servers = set(exclude_servers)

    servers = {}
    for key in inventory:
        m = _BACKUP_INFO_RE.match(key)
        if m:
            s = servers.setdefault(m.group("server"), _empty_server())
            s["backups"][m.group("backup_id")] = key
            continue
        m = _BASE_MEMBER_RE.match(key)
        if m:
            s = servers.setdefault(m.group("server"), _empty_server())
            s["members"].setdefault(m.group("backup_id"), []).append(key)
            continue
        m = _WAL_MEMBER_RE.match(key)
        if m:
            s = servers.setdefault(m.group("server"), _empty_server())
            s["wals"].append(key)
            continue
        d.unclassified.append(key)

    for server, s in sorted(servers.items()):
        if server in excluded_servers:
            d.orphans.extend(sorted(s["backups"].values()))
            for keys in s["members"].values():
                d.orphans.extend(sorted(keys))
            d.orphans.extend(sorted(s["wals"]))
            d.detail.setdefault("servers", {})[server] = {"excluded_by_operator": True}
            continue

        server_detail = {"backups": {}, "wal_objects": len(s["wals"])}

        for backup_id in sorted(set(s["members"]) - set(s["backups"])):
            d.warnings.append(
                "{}/base/{}/ has objects but no backup.info; copied anyway, and barman "
                "will not list it".format(server, backup_id)
            )
            for key in s["members"][backup_id]:
                d.live[key] = inventory[key]
            server_detail["backups"][backup_id] = {"status": "NO_BACKUP_INFO"}

        for backup_id, info_key in sorted(s["backups"].items()):
            body = _fetch_catalogue(get_object, info_key)
            if body is None:
                # barman's retention deleted the backup mid-pass. Its member objects are
                # copied anyway: the corpus's asymmetry says carrying residue costs disk
                # and dropping a live member costs a recovery.
                d.vanished.append(info_key)
                d.warnings.append(
                    "{} vanished from the source between the listing and the catalogue "
                    "read; its member objects are copied but barman will not list "
                    "them".format(info_key)
                )
                for key in s["members"].get(backup_id, []):
                    d.live[key] = inventory[key]
                server_detail["backups"][backup_id] = {"status": "VANISHED"}
                continue
            d.live[info_key] = inventory[info_key]
            d.catalogue_digests[info_key] = hashlib.sha256(body).hexdigest()
            info = parse_backup_info(info_key, body)

            status = info.get("status")
            if status != "DONE":
                d.warnings.append(
                    "{}/base/{} has status {!r}; copied anyway".format(
                        server, backup_id, status
                    )
                )

            members = s["members"].get(backup_id, [])
            if not members:
                d.holes.append(
                    Hole(
                        key="{}/base/{}/data.tar*".format(server, backup_id),
                        referenced_by=info_key,
                        detail="backup.info exists but no data objects are present",
                    )
                )
            for key in members:
                d.live[key] = inventory[key]

            server_detail["backups"][backup_id] = {
                "status": status,
                "timeline": info.get("timeline"),
                "begin_wal": info.get("begin_wal"),
                "end_wal": info.get("end_wal"),
                "xlog_segment_size": info.get("xlog_segment_size"),
                "objects": len(members),
            }

        for key in s["wals"]:
            d.live[key] = inventory[key]

        server_detail["wal_analysis"] = analyse_wal_chain(
            s["wals"], server_detail["backups"]
        )
        d.detail.setdefault("servers", {})[server] = server_detail

    return d


def _empty_server():
    return {"backups": {}, "members": {}, "wals": []}


def _segment_number(logid, segno, segment_size):
    segments_per_log = (1 << 32) // segment_size
    return logid * segments_per_log + segno


def _format_segment(timeline, absolute, segment_size):
    segments_per_log = (1 << 32) // segment_size
    return "{:08X}{:08X}{:08X}".format(
        timeline, absolute // segments_per_log, absolute % segments_per_log
    )


def analyse_wal_chain(wal_keys, backups, default_segment_size=None):
    segment_size = default_segment_size or DEFAULT_XLOG_SEGMENT_SIZE
    for meta in backups.values():
        declared = meta.get("xlog_segment_size")
        if declared:
            segment_size = int(declared)
            break

    timelines = {}
    histories = set()
    unrecognised = []
    for key in wal_keys:
        basename = strip_wal_suffix(key.rsplit("/", 1)[-1])
        m = _HISTORY_RE.match(basename)
        if m:
            histories.add(int(m.group(1), 16))
            continue
        m = _WAL_SEGMENT_RE.match(basename)
        if m:
            timeline = int(m.group(1), 16)
            absolute = _segment_number(
                int(m.group(2), 16), int(m.group(3), 16), segment_size
            )
            timelines.setdefault(timeline, set()).add(absolute)
            continue
        if not _XLOG_RE.match(basename):
            unrecognised.append(key)

    gaps = []
    per_timeline = {}
    for timeline, segments in sorted(timelines.items()):
        lo, hi = min(segments), max(segments)
        missing = sorted(set(range(lo, hi + 1)) - segments)
        per_timeline[timeline] = {
            "segments": len(segments),
            "first": _format_segment(timeline, lo, segment_size),
            "last": _format_segment(timeline, hi, segment_size),
            "missing": len(missing),
        }
        for absolute in missing:
            gaps.append(
                {
                    "timeline": timeline,
                    "segment": _format_segment(timeline, absolute, segment_size),
                    "kind": "INTERIOR_GAP",
                }
            )

    for backup_id, meta in sorted(backups.items()):
        begin, end = meta.get("begin_wal"), meta.get("end_wal")
        if not begin or not end:
            continue
        mb, me = _WAL_SEGMENT_RE.match(begin), _WAL_SEGMENT_RE.match(end)
        if not mb or not me:
            continue
        timeline = int(mb.group(1), 16)
        lo = _segment_number(int(mb.group(2), 16), int(mb.group(3), 16), segment_size)
        hi = _segment_number(int(me.group(2), 16), int(me.group(3), 16), segment_size)
        have = timelines.get(timeline, set())
        for absolute in range(lo, hi + 1):
            if absolute not in have:
                gaps.append(
                    {
                        "timeline": timeline,
                        "segment": _format_segment(timeline, absolute, segment_size),
                        "kind": "BACKUP_NOT_COVERED",
                        "backup": backup_id,
                    }
                )

    missing_histories = []
    if timelines:
        for timeline in sorted(timelines):
            if timeline > min(timelines) and timeline not in histories:
                missing_histories.append("{:08X}.history".format(timeline))

    return {
        "segment_size": segment_size,
        "timelines": per_timeline,
        "histories": sorted("{:08X}.history".format(t) for t in histories),
        "missing_histories": missing_histories,
        "gaps": gaps,
        "unrecognised_objects": unrecognised,
    }
