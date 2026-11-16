---
title: Disaster recovery and backups
section: backend
order: 71
phase: "Phase 9 · DevOps and observability"
summary: RPO and RTO, backup types, PostgreSQL point-in-time recovery with WAL archiving, the 3-2-1 rule, restore drills, and four multi-region DR strategies.
---

High availability and disaster recovery solve different problems. HA (replicas, Multi-AZ, failover) keeps you running when a machine or zone dies. It does nothing for a `DELETE` without a `WHERE`, a migration that corrupts a column, ransomware with production credentials, or a bug that writes garbage for six hours, because replication copies the damage to every replica in milliseconds. Disaster recovery is the plan and the machinery for getting back to a known-good state after those events, including losing a whole region. Its core is backups you have actually restored, and numbers the business agreed to before the disaster.

## What you are protecting against

| Scenario | Example | What saves you |
|---|---|---|
| Disk or instance failure | EBS volume degraded | Replication, Multi-AZ failover |
| Availability zone outage | Power or network loss in one AZ | Multi-AZ deployment |
| Region outage | Regional control plane or network failure | Cross-region replica or backups, multi-region strategy |
| Human or application error | Bad migration, buggy job, accidental `DROP TABLE` | Point-in-time recovery, delayed replica |
| Malicious action | Ransomware, compromised admin deletes backups | Immutable backups in a separate account |
| Provider or account loss | Account suspended, billing lockout | Copies outside the account or provider |

Replication handles the first three rows quickly. Only backups handle the last three.

## RPO and RTO

```text
   last good                 disaster              service restored
   recovery point               |                          |
 ------|------------------------X--------------------------|------> time
       |<-------- RPO --------->|<--------- RTO ---------->|
            data you lose                 downtime
```

- **RPO (recovery point objective)**: how much data, measured in time, you can afford to lose. Set by how often data leaves the failure domain: nightly backups mean up to 24 hours of loss; continuous WAL archiving means seconds to minutes; synchronous cross-region replication means near zero, at a write-latency cost.
- **RTO (recovery time objective)**: how long you can be down. Set by the whole recovery path: detection, decision, provisioning, restoring data, replaying logs, rewiring DNS and connections, warming caches, verifying.

Both are business decisions with a cost curve: each step toward zero costs roughly an order of magnitude more. Tier services: the payment ledger may need RPO near zero and RTO of minutes; the analytics warehouse can tolerate RPO of a day and RTO of a day.

RTO is usually dominated by data size. Restoring a 2 TB database at 400 MB/s takes about 90 minutes before WAL replay even starts; if the agreed RTO is one hour, the backup strategy can't meet it no matter how good the runbook is. The runnable example under "In practice" does this arithmetic.

## Backup types

| Type | What it is | Restore speed | Notes |
|---|---|---|---|
| Full | Complete copy of the data | Fastest to restore (one artifact) | Largest, slowest to take |
| Incremental | Changes since the last backup of any type | Slowest: full plus every incremental in the chain | Smallest and fastest to take |
| Differential | Changes since the last full | Full plus one differential | Grows until the next full |
| Logical (`pg_dump`) | SQL or archive of schema and rows | Slow for big databases (rebuilds indexes) | Portable across major versions, per table or per tenant; a single point in time, no PITR |
| Physical (`pg_basebackup`, pgBackRest) | Copy of data files | Fast, block level | Same major version and architecture; basis for PITR |
| Storage snapshot | EBS or disk snapshot | Fast to create, lazy load on restore | Crash-consistent unless the DB is quiesced; same-provider |
| Continuous | Base backup plus archived WAL | Restore to any second within retention | The standard for production PostgreSQL |

A common PostgreSQL schedule with pgBackRest: weekly full, daily differential or incremental, continuous WAL archiving, all to object storage with retention such as 14–35 days for PITR. On top of that, **grandfather-father-son** (GFS) retention keeps daily backups for a week, weekly for a month, monthly for a year, so long-term copies exist without storing every day forever.

Logical dumps still have a place: per-tenant exports in a multi-tenant SaaS (restore one customer without touching the others), schema-level archives before major upgrades, and data portability. They're not a disaster recovery strategy for a multi-terabyte database.

Back up more than the main database: object storage (S3 versioning plus replication), Redis if it holds anything you can't rebuild, search indexes (or the ability to rebuild them, and the time that takes), Kafka topics that are a source of truth, secrets and encryption keys, and infrastructure code and configuration. An encrypted backup is worthless if the KMS key that decrypts it was deleted with the account.

## Point-in-time recovery with WAL archiving

PostgreSQL writes every change to the write-ahead log (WAL) before changing data files. WAL is split into 16 MB segments. If you keep a **base backup** (a physical copy of the data directory) and **every WAL segment since**, you can rebuild the database as of any moment after the base backup by replaying WAL up to that moment.

```text
 Sun 02:00                        Wed 09:41:07 bad DELETE
 base backup                                |
 [== physical copy of data dir ==]          |
 WAL: 0001 0002 0003 ... 01A6 01A7 01A8 ... 01B0   (16 MB each, archived)
      |--------------- replay ---------------|
 restore: copy base backup, replay WAL until 09:41:00, then promote
```

How it's set up:

1. `wal_level = replica` (or `logical`), `archive_mode = on`, and an `archive_command` (or, since PostgreSQL 15, an `archive_library`) that copies each completed segment to durable storage. Use a tool built for this, such as pgBackRest, WAL-G or Barman, rather than a hand-written `cp` or `aws s3 cp`: they handle compression, encryption, parallelism, checksums and retries.
2. **Bound RPO on quiet databases** with `archive_timeout` (for example 60 seconds), which forces a segment switch so a low-traffic database still ships WAL regularly. Data not yet archived is what you lose if the primary and its disks vanish; a streaming replica in another zone or region narrows that further.
3. **Monitor archiving.** If `archive_command` fails, PostgreSQL keeps the WAL in `pg_wal` and retries, so a broken archive both silently breaks PITR and eventually fills the primary's disk. Alert on `pg_stat_archiver.failed_count` increasing and on the age of `last_archived_time`.

How restore works (PostgreSQL 12+): restore the base backup into an empty data directory, set `restore_command` (how to fetch archived segments) and a target such as `recovery_target_time`, create a `recovery.signal` file, and start the server. It replays WAL up to the target, then pauses or, with `recovery_target_action = 'promote'`, opens for writes on a new **timeline**, so the WAL history after the restore point doesn't collide with the old one. Tools wrap all of this in one command.

For recovering from human error, don't overwrite production in place. Restore to a **new instance** at a point just before the mistake, verify, then either switch the application over or copy the lost rows back into production. Managed databases do the same: RDS and Aurora offer PITR within the backup retention window (up to 35 days), always into a new instance. A **delayed replica** (`recovery_min_apply_delay = '1h'`) is a complementary trick: a standby that lags by an hour, so a bad `DROP` can be caught before it replays there.

## The 3-2-1 rule

Keep at least **3** copies of the data, on **2** different kinds of storage, with **1** copy offsite. It guards against correlated failures: one copy is production, the others must not share its failure modes.

In cloud terms: production database, backups in object storage, and a copy in another region **and another account**. The account boundary matters most today, because the biggest threat to backups is credentials: an attacker or a buggy script with admin access to the production account can delete snapshots and buckets in the same account just as easily as the database.

The modern extension is **3-2-1-1-0**: one copy **immutable** or air-gapped (S3 Object Lock in compliance mode, AWS Backup Vault Lock: nobody, including the root user, can delete it before retention expires), and **0** errors in verified restore tests.

## Restore drills

A backup that has never been restored is a hope, not a backup. Backups fail silently in ordinary ways: the job has been failing for weeks and the alert went to an old email list; the archive is missing WAL segments; the backup is encrypted with a deleted key; the restore takes 9 hours against a 1-hour RTO; the runbook references a tool nobody has installed. GitLab's public postmortem of its January 2017 database incident is the standard lesson: an engineer removed the primary's data directory by mistake, several of the backup and replication mechanisms turned out not to be working, and about six hours of data was lost after restoring from a staging snapshot.

What a good drill program looks like:

- **Automated, frequent restores**: nightly or weekly, restore the latest backup to a scratch instance, at a random point in time within the window.
- **Verify, don't just start**: the server starts, `pg_amcheck` finds no corruption, row counts and recent-data checks match expectations (rows from the last hour exist), and an application smoke test passes against it.
- **Measure**: record restore duration and data age every time. That is your real RTO and RPO, and the trend shows when growth will break the RTO.
- **Alert on drill failure** like a production incident.
- **Game days** for the full scenario a few times a year: lose a region in staging (or carefully in production), follow the runbook as written, time it, and fix what was wrong in the runbook.

## Multi-region strategies

AWS's DR guidance popularized four strategies on a cost and recovery spectrum. They apply to any cloud.

```text
 cheaper, slower                                     costlier, faster
 <------------------------------------------------------------------->
 backup & restore    pilot light      warm standby      active-active
 data: backups       data: live       data: live        data: live,
 infra: none         replica          replica           multi-writer
                     infra: off or    infra: running,   or partitioned
                     minimal          scaled down       infra: full, all
 RPO/RTO: hours      tens of minutes  minutes           near zero
```

| Strategy | Standing in the DR region | RPO | RTO | Cost | Complexity |
|---|---|---|---|---|---|
| Backup and restore | Backups and IaC only | Hours (or minutes with cross-region WAL archives) | Hours | Lowest | Low, but recovery is a big manual event |
| Pilot light | Live data replication; compute defined in IaC but off or minimal | Seconds to minutes | Tens of minutes | Low | Moderate |
| Warm standby | A full but scaled-down copy, running and tested | Seconds | Minutes | Medium | Moderate to high |
| Multi-site active-active | Full capacity serving traffic in every region | Near zero | Near zero | Highest | Highest: data conflicts, routing, consistency |

- **Backup and restore**: copy backups and WAL archives cross-region; on disaster, apply IaC in the new region, restore data, deploy, switch DNS. Requires that the IaC actually works there (AMIs, images, quotas, KMS keys, secrets replicated).
- **Pilot light**: a cross-region read replica runs continuously (RDS cross-region replica, Aurora Global Database); application servers are defined but scaled to zero. Recovery: promote the replica, scale up compute, switch traffic.
- **Warm standby**: everything runs at small scale and serves synthetic or a small amount of real traffic, which proves it works. Recovery: promote data, scale out, shift traffic.
- **Active-active**: every region serves users. The hard part is data: either partition so each record has one home region (tenant pinned to a region, writes routed there), or accept multi-writer conflict resolution (last-writer-wins, CRDTs) for data that tolerates it. Strongly consistent global writes need consensus across regions and pay cross-region latency on every write (see [CAP and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) and [replication]({{ '/backend/replication/' | relative_url }})).

Failover mechanics that decide whether it works on the day:

- **Traffic switch**: DNS failover with health checks and short TTLs (30–60 seconds), or a global load balancer or anycast front door that shifts traffic without waiting for client caches.
- **Database promotion and fencing**: make sure the old primary can't keep accepting writes when the network heals (split brain). Promotion should be a deliberate, ideally single-command step, not something that happens on one failed health check.
- **Static stability**: the recovery path shouldn't depend on the failed region, on control planes that may be degraded during a large event, or on scaling up from zero when everyone else is doing the same; pre-provisioned capacity and pre-raised quotas avoid that.
- **Regional dependencies**: container registry, secrets, KMS keys, IAM, third-party allowlists of your egress IPs, and anything hardcoded to a regional endpoint.
- **Failback**: returning to the primary region needs re-replication in the other direction and is a second, planned migration. Plan and practice it too.

## In practice

Illustrative PostgreSQL archiving with pgBackRest:

```ini
# postgresql.conf
wal_level = replica
archive_mode = on
archive_command = 'pgbackrest --stanza=main archive-push %p'
archive_timeout = 60
```

```bash
# scheduled backups (cron or a Kubernetes CronJob)
pgbackrest --stanza=main --type=full backup     # weekly
pgbackrest --stanza=main --type=diff backup     # daily
pgbackrest --stanza=main info                   # what exists, oldest PITR point

# on a new (or stopped) instance, restore to just before the bad DELETE;
# --delta reuses files that already match instead of copying everything
pgbackrest --stanza=main --delta --type=time \
  "--target=2026-03-14 09:41:00+00" --target-action=promote restore

# managed equivalent: always restores into a new instance
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier orders-prod \
  --target-db-instance-identifier orders-pitr-0941 \
  --restore-time 2026-03-14T09:41:00Z
```

Archiving health, for an alert rule or a dashboard:

```sql
SELECT archived_count,
       failed_count,
       last_failed_wal,
       now() - last_archived_time AS since_last_archive
FROM pg_stat_archiver;
```

A runnable GFS retention selector and RTO estimate:

```python
from datetime import date, timedelta


def gfs_keep(backups, daily=7, weekly=4, monthly=12):
    """Grandfather-father-son retention: which backup dates to keep.

    Keeps the newest `daily` backups, plus the newest backup of each of the
    last `weekly` ISO weeks and of each of the last `monthly` months.
    """
    newest_first = sorted(backups, reverse=True)
    keep = set(newest_first[:daily])
    for period, limit in ((lambda d: d.isocalendar()[:2], weekly),
                          (lambda d: (d.year, d.month), monthly)):
        last_in_period = {}
        for d in newest_first:
            last_in_period.setdefault(period(d), d)    # first seen = newest
        keep.update(list(last_in_period.values())[:limit])
    return keep


def restore_minutes(db_gb, restore_mb_s, wal_gb, replay_mb_s, fixed_min):
    """Rough RTO: copy the base backup, replay WAL, then fixed steps."""
    copy = db_gb * 1024 / restore_mb_s / 60
    replay = wal_gb * 1024 / replay_mb_s / 60
    return copy + replay + fixed_min


if __name__ == "__main__":
    today = date(2026, 11, 15)
    backups = [today - timedelta(days=i) for i in range(400)]
    keep = gfs_keep(backups)

    assert today in keep and today - timedelta(days=6) in keep   # dailies
    assert today - timedelta(days=10) not in keep               # mid-week, not kept
    assert date(2026, 10, 31) in keep                            # month-end
    assert date(2025, 12, 31) in keep                            # 12th month back
    assert date(2025, 11, 30) not in keep                        # 13th month: gone
    sundays = [d for d in keep if d.isoweekday() == 7 and (today - d).days < 28]
    assert len(sundays) == 4                                     # weekly fathers
    assert len(keep) <= 7 + 4 + 12

    # 2 TB database restored at 400 MB/s, 300 GB of WAL replayed at 100 MB/s,
    # plus 20 minutes to provision, verify and repoint the app
    rto = restore_minutes(2048, 400, 300, 100, 20)
    assert 155 < rto < 160, rto      # about 2 h 40 min: does that meet the RTO?
    print("dr ok", len(keep), round(rto))
```

If the answer is "no, the RTO is one hour", the options are a warm standby you promote instead of restoring, more frequent base backups so less WAL needs replaying, faster restore paths (snapshots, parallel restore), or splitting the database so each part restores independently.

## Pitfalls

- **Treating replicas as backups.** Symptom: a bad migration or `DELETE` replicates everywhere instantly. Fix: PITR backups, optionally a delayed replica.
- **Backups in the same account and region.** Symptom: ransomware or a compromised admin deletes production and backups together. Fix: cross-account, cross-region copies, Object Lock or vault lock.
- **Silent archive failure.** Symptom: PITR window has gaps; eventually the primary's disk fills with unarchived WAL. Fix: alert on `pg_stat_archiver` failures and archive age.
- **Never restored.** Symptom: the first restore happens during a disaster and fails or takes 10x longer than planned. Fix: automated restore drills with verification and timing.
- **Lost keys.** Symptom: backups exist but can't be decrypted because the KMS key or the backup tool's passphrase is gone. Fix: back up keys and credentials separately, with their own access controls; protect KMS keys from deletion.
- **RTO that ignores data growth.** Symptom: the restore that took 40 minutes two years ago now takes 4 hours. Fix: track drill duration as a metric; change strategy before it crosses the RTO.
- **Forgotten data stores.** Symptom: the database is back but uploaded files, Redis-held state or search indexes are gone. Fix: inventory every stateful component with its own RPO, RTO and backup method.
- **Untested failover and failback.** Symptom: DNS TTLs of a day, missing quotas, secrets not replicated, split brain. Fix: game days with written, timed runbooks.

## Interview questions

**What is the difference between RPO and RTO?** RPO is the maximum acceptable data loss measured in time, set by how often data leaves the failure domain. RTO is the maximum acceptable downtime, set by the whole recovery path. Both are business decisions with steep costs toward zero.

**Why aren't replicas a backup?** Replication faithfully copies every change, including deletes, bad migrations and corruption from bugs. You need copies you can go back in time with: PITR backups, immutable snapshots, or at least a delayed replica.

**How does point-in-time recovery work in PostgreSQL?** Take periodic physical base backups and continuously archive WAL segments. To recover, restore a base backup, fetch and replay archived WAL up to the target time, and promote, which starts a new timeline. RPO is bounded by unarchived WAL, controlled with `archive_timeout` and replicas.

**Full vs incremental vs differential backups?** Full copies everything; incremental copies changes since the last backup of any kind (smallest, but restore needs the whole chain); differential copies changes since the last full (restore needs the full plus one differential).

**What is the 3-2-1 rule, and how does it apply in the cloud?** Three copies, two kinds of storage, one offsite. In the cloud: production, backups in object storage, and a copy in a different region and account, ideally immutable with object lock, because credential compromise is the main threat.

**How would you design DR for a SaaS with a 15-minute RTO and 1-minute RPO?** Warm standby: continuous cross-region replication of the database (replica or global database), a scaled-down running copy of the application stack in the second region, IaC and images ready, DNS or global load balancer failover with short TTLs, a rehearsed promotion runbook with fencing, plus separate PITR backups for logical errors.

**How do you know your backups work?** Automated scheduled restores into scratch environments with integrity checks, data freshness checks and an application smoke test, timed and alerted on, plus periodic game days of the full regional scenario.

## Further reading

- PostgreSQL documentation: "Continuous Archiving and Point-in-Time Recovery (PITR)" in the Backup and Restore chapter.
- AWS whitepaper: *Disaster Recovery of Workloads on AWS: Recovery in the Cloud*.
- pgBackRest User Guide.
- *Site Reliability Engineering* (Google, 2016), chapter "Data Integrity: What You Read Is What You Wrote".
- GitLab, *Postmortem of database outage of January 31* (2017).
