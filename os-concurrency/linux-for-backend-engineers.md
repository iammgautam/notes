---
title: Linux for backend engineers
section: os-concurrency
order: 7
summary: File descriptors, signals and graceful shutdown in containers, ulimits, and the top, ss, lsof and strace commands that answer production questions fast.
---

When a dashboard says "something is wrong" and logs say nothing useful, you end up on the box or inside the container. A small set of Linux concepts (file descriptors, signals, limits) and a dozen commands answer most questions: what is this process doing, what is it waiting on, who is it talking to, why did it die, and why did the deploy drop requests.

## How it works

### File descriptors

Everything a process does I/O on is a **file descriptor** (fd): an integer index into its fd table. Regular files, sockets, pipes, epoll instances, eventfds. 0, 1, 2 are stdin, stdout, stderr. Each TCP connection your app holds (clients, Postgres, Redis, outbound HTTP) is one fd.

Limits:

- Per process: `RLIMIT_NOFILE` (`ulimit -n`). Often 1024 soft by default; inside containers it is whatever the runtime and orchestrator set, so check it instead of assuming.
- System-wide: `fs.file-max`.

When exhausted, `accept()` and `open()` fail with `EMFILE: Too many open files`. The usual cause is not the limit but a **leak**: files or connections opened and never closed.

### Signals

Signals are asynchronous notifications to a process.

| Signal | Number | Default | Typical meaning |
|---|---|---|---|
| SIGTERM | 15 | terminate | "please shut down" (Docker, Kubernetes, systemd) |
| SIGINT | 2 | terminate | Ctrl-C |
| SIGKILL | 9 | kill, uncatchable | OOM killer, `kill -9`, grace period expired |
| SIGHUP | 1 | terminate | reload config (gunicorn, nginx) |
| SIGQUIT | 3 | core dump | Celery: cold shutdown; gunicorn: quick shutdown |
| SIGCHLD | 17 | ignore | child exited, parent should reap it |
| SIGUSR1/2 | 10/12 | terminate | app-defined (log reopen, gunicorn upgrade) |

Exit codes above 128 mean "killed by signal N": 137 = SIGKILL, 143 = SIGTERM.

### Graceful shutdown

A rolling deploy sends SIGTERM, waits a grace period (Kubernetes `terminationGracePeriodSeconds`, default 30 s; `docker stop -t`, default 10 s), then SIGKILL. A graceful server should:

1. Stop accepting new connections (and fail readiness so the LB stops routing).
2. Finish in-flight requests or tasks.
3. Close pools and flush buffers.
4. Exit before the grace period ends.

gunicorn on SIGTERM stops accepting and gives workers `--graceful-timeout` seconds. Celery on SIGTERM does a warm shutdown: stops consuming, finishes current tasks. Long tasks need `acks_late=True` so a SIGKILL mid-task leads to redelivery, not loss, which also means tasks must be idempotent.

Kubernetes removes the pod from Service endpoints **at the same time** it sends SIGTERM, and that propagation takes a few seconds. Add a short `preStop` sleep so the LB stops sending traffic before your server stops accepting.

### PID 1 in containers

The container's main process is PID 1, which gets special treatment: the kernel doesn't apply default signal actions to it, so if it doesn't install a SIGTERM handler, SIGTERM does nothing and you wait for the SIGKILL. PID 1 also must reap zombie children.

Two common mistakes:

- **Shell form** `CMD gunicorn ...` runs `/bin/sh -c "gunicorn ..."`. The shell is PID 1 and doesn't forward SIGTERM. Use exec form.
- Entrypoint scripts that don't `exec` the final command.

```text
# Dockerfile
CMD ["gunicorn", "config.wsgi:application", "-c", "gunicorn.conf.py"]
# or with a tiny init that forwards signals and reaps zombies
ENTRYPOINT ["tini", "--"]
```

```bash
#!/bin/sh
# entrypoint.sh
set -e
python manage.py migrate --noinput
exec "$@"        # replace the shell so the app becomes PID 1
```

`docker run --init` adds tini for you.

## In practice

### What is the process doing?

```bash
top -o %CPU           # press 1 for per-core, H for threads
htop                  # F5 tree view shows gunicorn master and workers
ps -eo pid,ppid,stat,%cpu,rss,etime,cmd --sort=-%cpu | head
pidstat -u -r -p <pid> 1
py-spy dump --pid <pid>         # Python stacks of all threads, no restart
py-spy top --pid <pid>          # live profiler
```

Load average counts runnable **and** uninterruptible (`D` state, usually disk or NFS I/O) tasks. Load 8 on 8 cores at low CPU usually means I/O wait; check `%wa` in `top` and `iostat -x 1`.

Process states in `ps STAT`: `R` running, `S` sleeping (waiting on an event), `D` uninterruptible I/O, `Z` zombie, `T` stopped.

### Who is it talking to?

```bash
ss -tlnp                         # listening TCP sockets + owning process
ss -tnp state established '( dport = :5432 )'    # connections to Postgres
ss -s                            # summary: how many in each state
ss -tn state time-wait | wc -l   # many TIME_WAIT = no connection reuse
ss -ltn 'sport = :8000'          # Recv-Q on a listener = accept backlog depth
```

On a listening socket, `Recv-Q` is connections waiting for `accept()`. If it sits near `Send-Q` (the backlog size), workers are saturated and new connections get dropped or delayed.

### Which files and sockets are open?

```bash
lsof -p <pid> | wc -l            # fd count
ls /proc/<pid>/fd | wc -l        # same, no lsof needed
lsof -p <pid> -a -i              # only network fds
lsof -i :8000                    # who owns port 8000
lsof +L1                         # deleted files still held open (disk not freed)
cat /proc/<pid>/limits | grep 'open files'
```

"Disk full, but `du` shows nothing big" is almost always a deleted log file still held open by a process. Restart or signal it to reopen logs.

### What system calls is it making?

```bash
strace -f -p <pid> -e trace=network,read,write -T   # -T: time spent per call
strace -c -f -p <pid>            # summary table; Ctrl-C to stop
strace -f -e trace=openat python manage.py check 2>&1 | grep -v ENOENT
```

A process stuck in `futex(...)` is waiting on a lock; in `recvfrom` on a socket fd is waiting for a peer (check which with `lsof`); in `epoll_wait` is an idle event loop. `strace` slows the target a lot, so use it briefly in production. In containers it needs `SYS_PTRACE`.

### Limits

```bash
ulimit -n                        # soft limit for this shell
ulimit -Hn                       # hard limit
prlimit --pid <pid> --nofile     # limits of a running process
sysctl net.core.somaxconn        # max accept backlog
sysctl net.ipv4.ip_local_port_range   # ephemeral ports for outbound connections
```

```yaml
# docker-compose.yml
services:
  web:
    ulimits:
      nofile: { soft: 65536, hard: 65536 }
    stop_grace_period: 45s
```

Running out of ephemeral ports (`Cannot assign requested address` on outbound connects) comes from opening a new connection per outbound call; the fix is connection reuse, not kernel tuning.

### Logs and kernel messages

```bash
journalctl -u myapp -f --since "10 min ago"
dmesg -T | tail -50             # OOM kills, segfaults, disk errors
docker logs --since 10m -f web
kubectl logs <pod> --previous   # logs of the crashed container
```

## Pitfalls

- **Shell-form CMD** swallowing SIGTERM; every deploy waits for the grace period, then SIGKILLs in-flight requests.
- **Grace periods shorter than your longest request or task**, so long uploads, LLM streams and Celery tasks get killed mid-work.
- **fd leaks**: `open()` without `with`, a new `requests.Session` per call never closed, HTTP clients created per request. Watch fd count over time.
- **Assuming `ulimit` inside the container equals the host's.**
- **Zombie processes** piling up under a PID 1 that doesn't reap (subprocess-heavy workers). Use `tini`.
- **Running `strace` or `py-spy` without the right capability** in a container and concluding the process is fine.
- **Ignoring `D` state**: a process stuck in uninterruptible I/O can't even be killed with SIGKILL until the I/O returns.

## Checklist

- Exec-form `CMD`, `exec "$@"` in entrypoints, or `tini` as PID 1.
- Handle SIGTERM: stop accepting, drain, exit within the grace period; `preStop` sleep on Kubernetes.
- Grace period longer than your longest request or task; `acks_late` plus idempotent Celery tasks.
- Raise `nofile` for services with many connections; alert on fd count.
- Know your go-to commands: `top`, `ss -tlnp`, `lsof -p`, `strace -c`, `py-spy dump`, `dmesg -T`.

## Interview angle

- **"What is a file descriptor?"** A per-process integer handle to an open file, socket or pipe in the kernel; every connection consumes one, and the per-process limit is `ulimit -n`.
- **"How do you shut down a web server gracefully in Kubernetes?"** Handle SIGTERM, fail readiness, keep serving briefly while endpoints update (`preStop` sleep), stop accepting, drain in-flight requests, close resources, exit before the grace period ends.
- **"SIGTERM vs SIGKILL?"** SIGTERM can be caught for cleanup; SIGKILL cannot and ends the process immediately. Orchestrators send SIGTERM first, then SIGKILL after the grace period.
- **"A service says 'Too many open files'. What do you do?"** Check the fd count and limit in `/proc/<pid>/limits`, use `lsof -p` to see what kind of fds are piling up, fix the leak (usually unclosed connections), and raise the limit if the steady-state need is legitimately high.
