---
title: Kubernetes and container orchestration
section: backend
order: 65
phase: "Phase 9 · DevOps and observability"
summary: Orchestration (scheduling, self-healing, rolling updates), core Kubernetes objects, probes, requests and limits, and HPA math, custom metrics and KEDA.
---

Once you run more than a handful of containers, someone has to decide which machine each one runs on, restart it when it dies, replace it when the machine dies, route traffic only to healthy copies, roll out new versions without downtime, and add copies when load rises. That someone is an orchestrator. Kubernetes is the dominant one: you declare the state you want in YAML, and a set of control loops keeps working to make the cluster match it. Knowing what those loops do, and which knobs feed them, explains most production incidents on Kubernetes: pods stuck `Pending`, crash loops, OOM kills, throttled latency, autoscalers that never scale.

## Container orchestration

Every orchestrator is built from the same parts: a store of desired state, a scheduler that places work on machines, agents on each machine that run it, and controllers that compare desired with observed state and act on the difference. Kubernetes calls this reconciliation, and it is level-triggered: a controller doesn't react to "a pod died" as an event, it notices "3 desired, 2 running" and creates one, which is why it recovers from missed events and restarts of its own.

```text
                 kubectl / CI / Argo CD
                          |
                          v
   +--------------------- control plane ---------------------+
   |  kube-apiserver <----> etcd (desired + observed state)   |
   |       ^    ^                                             |
   |       |    +---- scheduler: picks a node for new pods    |
   |       +--------- controllers: Deployment, ReplicaSet,    |
   |                  HPA, Job, Node ... (reconcile loops)    |
   +----------------------------------------------------------+
            |  watch                         |  watch
            v                                v
   +---------- node 1 ----------+  +---------- node 2 ----------+
   | kubelet -> containerd      |  | kubelet -> containerd      |
   | [pod api-7f9] [pod api-2c] |  | [pod api-x1] [pod worker]  |
   | kube-proxy / CNI dataplane |  | kube-proxy / CNI dataplane |
   +----------------------------+  +----------------------------+
```

Only the API server talks to etcd. Everything else, including the scheduler, controllers and kubelets, watches the API server and writes back through it.

### Scheduling

The scheduler takes each unscheduled pod and picks a node in two phases:

1. **Filter.** Drop nodes that can't run the pod: not enough unreserved CPU or memory for the pod's **requests**, a `nodeSelector` or node affinity that doesn't match, taints the pod doesn't tolerate, volume zone constraints, port conflicts.
2. **Score.** Rank the remaining nodes: spread replicas across nodes and zones, prefer nodes with the image already pulled, balance utilization, honor preferred affinities.

Scheduling uses requests, not actual usage: a 10%-busy node can reject a pod because its requests are fully booked, and a pod with no requests fits anywhere and can starve its neighbors. If nothing fits, the pod stays `Pending` with a `FailedScheduling` event, the signal for a cluster autoscaler (or Karpenter) to add a node. `topologySpreadConstraints` spread replicas across zones; priority classes let critical pods preempt others.

### Self-healing

- **Container crash**: the kubelet restarts it per `restartPolicy`, with exponential backoff from 10 seconds up to 5 minutes. A pod stuck in that backoff shows `CrashLoopBackOff`.
- **Failed liveness probe**: the kubelet kills and restarts the container.
- **Failed readiness probe**: the pod is removed from Service endpoints, with no restart.
- **Pod deleted or node lost**: the ReplicaSet controller sees fewer pods than desired and creates replacements. When a node stops reporting, its pods are evicted after a toleration period (300 seconds by default), so node failure recovery takes minutes, not seconds.
- **Voluntary disruptions** (node drains for upgrades, autoscaler scale-down) respect a **PodDisruptionBudget**, which caps how many replicas can be down at once.

### Rolling updates

Changing a Deployment's pod template creates a new ReplicaSet. The controller scales it up and the old one down in steps bounded by `maxSurge` (extra pods allowed above the desired count) and `maxUnavailable` (pods allowed below it), and only counts a new pod as available after its readiness probe passes (plus `minReadySeconds`). If new pods never become ready, the rollout stalls instead of taking down the old version, and `progressDeadlineSeconds` marks it failed. `kubectl rollout undo` scales the previous ReplicaSet back up. Strategy trade-offs, canaries and blue-green are in [deployment strategies]({{ '/backend/deployment-strategies/' | relative_url }}).

### Alternatives: ECS, Nomad and others

| Option | Model | Good fit | Trade-off |
|---|---|---|---|
| Kubernetes (EKS, GKE, AKS, self-managed) | Declarative API, huge ecosystem | Many services, platform team, portability | Operational and conceptual complexity |
| AWS ECS (on EC2 or Fargate) | Task definitions and services, AWS-native | AWS-only shops wanting less to run | AWS lock-in, smaller ecosystem |
| HashiCorp Nomad | Single binary, schedules containers, VMs and plain binaries | Mixed workloads, simpler operations | Smaller ecosystem; source-available license since 2023 |
| Cloud Run, App Runner, Fly.io | Serverless containers | Stateless HTTP services, scale to zero | Less control over networking and long-running work |
| Docker Swarm | Compose-like clustering | Very small setups | Low adoption, limited features |

For a small team with a few Django services on AWS, ECS on Fargate is often cheaper in engineering time. Kubernetes pays off with many services, a platform team, and needs like custom autoscaling, operators or portability.

## Kubernetes core objects

### Pods

A pod is the scheduling unit: one or more containers sharing a network namespace (one IP, `localhost` between them) and optionally volumes. Usually it is one app container plus sidecars such as a log shipper or proxy. Init containers run to completion before the app starts; native sidecars (GA in Kubernetes 1.33) start before and stop after it. Pods are disposable: a replacement gets a new name and IP, so nothing should address pods directly.

### Deployments

A Deployment manages ReplicaSets, which manage identical, interchangeable pods: the right shape for stateless APIs and workers. Other workload controllers cover other shapes: **StatefulSet** for stable names and per-replica volumes (databases, Kafka, ZooKeeper), **DaemonSet** for one pod per node (log and metrics agents), **Job** and **CronJob** for run-to-completion work (migrations, nightly reports).

### Services

A Service gives a stable virtual IP and DNS name (`orders-api.payments.svc.cluster.local`) to a changing set of pods chosen by label selector. Only ready pods are listed in its EndpointSlices; kube-proxy (or an eBPF dataplane such as Cilium) programs each node to translate the virtual IP to one of them.

| Type | Reachable from | Use |
|---|---|---|
| ClusterIP | Inside the cluster | Service-to-service calls (default) |
| NodePort | Each node's IP on a high port | Building block, rarely used directly |
| LoadBalancer | A cloud L4 load balancer | Exposing TCP or UDP services externally |
| Headless (`clusterIP: None`) | DNS returns pod IPs | StatefulSets, client-side balancing |

Service balancing is per connection. Long-lived HTTP/2 or gRPC connections stick to one pod; see [service discovery and mesh]({{ '/backend/service-discovery-and-mesh/' | relative_url }}).

### Ingress

An Ingress is an L7 routing rule (host and path to Service, plus TLS) implemented by an **ingress controller**: ingress-nginx, Traefik, HAProxy, or a cloud controller that provisions an ALB. One external load balancer then fronts many services. The newer **Gateway API** (GA since 2023) splits roles into GatewayClass, Gateway and HTTPRoute and supports weighted backends and header matching natively; with the community ingress-nginx controller announced for retirement in 2026, new clusters increasingly start there.

### ConfigMaps and Secrets

ConfigMaps hold non-secret configuration; Secrets hold credentials. Both reach the pod as environment variables or mounted files.

- **Env vars are read once at start.** Changing the ConfigMap does nothing until pods restart. A common trick is a checksum of the config in a pod template annotation, so a config change triggers a rollout.
- **Mounted files update eventually** (on the order of a minute, and never for `subPath` mounts). The app must re-read them.
- **Secrets are base64-encoded, not encrypted.** Anyone who can `get secrets` in the namespace can read them. Turn on encryption at rest for etcd with a KMS provider, restrict RBAC, and keep secrets out of Git, typically by syncing from a manager (AWS Secrets Manager, Vault) with the External Secrets Operator or the Secrets Store CSI driver. More in [API keys and secrets]({{ '/backend/api-keys-and-secrets/' | relative_url }}).

### Probes

| Probe | Question | On failure | Typical check |
|---|---|---|---|
| Startup | Has the app finished booting? | Keep waiting, then restart after the threshold | Same as liveness, with a long threshold |
| Readiness | Should this pod get traffic now? | Removed from endpoints, not restarted | App initialized, not draining, critical local dependencies usable |
| Liveness | Is the process wedged beyond recovery? | Container restarted | Event loop or worker responds; no dependency checks |

The cardinal rule: **liveness must not check shared dependencies.** If it queries Postgres, a 20-second database failover restarts the whole fleet at once. Readiness can consider dependencies carefully, but if every pod fails readiness together the Service has zero endpoints, which is an outage too. On shutdown, endpoint removal and `SIGTERM` happen concurrently, so add a short `preStop` sleep and drain in-flight requests within `terminationGracePeriodSeconds` (30 seconds by default). Details in [graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}).

### Resource requests and limits

- **Requests** are what the scheduler reserves and what CPU shares are based on under contention. They also define the denominator of HPA CPU utilization.
- **Limits** are hard caps. Exceeding the **memory** limit gets the container OOM-killed (exit 137). Exceeding the **CPU** limit gets it throttled by the CFS quota, which shows up as tail latency, not errors.

| QoS class | Rule | Eviction under node memory pressure |
|---|---|---|
| Guaranteed | Requests equal limits for CPU and memory in every container | Last |
| Burstable | Some requests set, not equal to limits | Middle, by usage over request |
| BestEffort | No requests or limits | First |

Common practice for web services: set CPU and memory requests from observed usage, set the memory limit equal to the request (memory isn't compressible, so overcommit means surprise kills), and consider omitting CPU limits on latency-sensitive services so they can burst into idle CPU. Each Gunicorn worker is a separate process, so memory requests scale with worker count. In multi-tenant clusters, `ResourceQuota` caps a namespace's total and `LimitRange` sets defaults.

## Horizontal Pod Autoscaling

The HorizontalPodAutoscaler (HPA) is a controller that adjusts a workload's replica count from metrics. It runs every 15 seconds by default.

### The HPA math

For each metric:

```text
desired = ceil( current_replicas * current_metric / target_metric )
```

For CPU, `current_metric` is average usage as a percentage of the pods' **requests**. Four pods averaging 90% of request with a 60% target gives `ceil(4 * 90 / 60) = 6`. Details that change behavior:

- **Tolerance.** If the ratio is within 10% of 1.0, nothing happens, which prevents flapping.
- **Several metrics**: each yields a proposal and the HPA takes the **maximum**.
- **Missing metrics and unready pods** are treated conservatively: when scaling up, pods without metrics are assumed to use 0%, and when scaling down, 100%, so gaps never cause an overreaction.
- **Scale-down stabilization.** By default the HPA uses the highest recommendation from the last 300 seconds, so a brief dip doesn't remove pods that are needed a minute later.
- **Rate limits.** The default scale-up policy allows the larger of +100% or +4 pods per 15 seconds; scale-down allows up to 100% per 15 seconds once stabilized. Both are tunable under `spec.behavior`.
- **Bounds**: `minReplicas` and `maxReplicas` clamp the result. Set `maxReplicas` from what downstream systems can take; 200 Django pods with 10 connections each is 2,000 Postgres connections.

A runnable simulation of the formula, tolerance, multi-metric max, stabilization window and scale-up cap:

```python
import math

TOLERANCE = 0.10          # kube-controller-manager default
SCALE_DOWN_WINDOW = 300   # seconds, default scale-down stabilization


def desired_for_metric(current, metric_value, target_value):
    """Core HPA formula for one metric: ceil(current * value / target)."""
    ratio = metric_value / target_value
    if abs(ratio - 1.0) <= TOLERANCE:
        return current                      # close enough: don't flap
    return math.ceil(current * ratio)


def desired_from_total(total_value, target_per_pod):
    """AverageValue target on an external metric, e.g. queue lag."""
    return math.ceil(total_value / target_per_pod)


class HPA:
    """Simplified controller: one sync every 15 s, default behavior."""

    def __init__(self, min_replicas, max_replicas):
        self.min, self.max = min_replicas, max_replicas
        self.history = []                   # (timestamp, recommendation)

    def sync(self, now, current, metrics):
        # several metrics: compute each proposal and take the largest
        raw = max(desired_for_metric(current, v, t) for v, t in metrics)
        raw = max(self.min, min(self.max, raw))

        self.history = [(ts, r) for ts, r in self.history
                        if now - ts < SCALE_DOWN_WINDOW]
        self.history.append((now, raw))
        # scale down only to the highest recommendation of the last 5 min
        down_limit = max(r for _, r in self.history)
        desired = max(current, raw)         # scale up immediately
        desired = min(desired, down_limit)

        # default scale-up policy per 15 s period: max(+100%, +4 pods)
        desired = min(desired, max(current * 2, current + 4))
        return max(self.min, min(self.max, desired))


if __name__ == "__main__":
    # 4 pods at 90% of their CPU request, target 60% -> ceil(4 * 1.5) = 6
    assert desired_for_metric(4, 90, 60) == 6
    # within 10% tolerance: no change
    assert desired_for_metric(6, 64, 60) == 6
    # queue lag 1,250 messages, target 100 per pod -> 13 pods
    assert desired_from_total(1250, 100) == 13

    hpa = HPA(min_replicas=2, max_replicas=20)
    # two metrics: CPU says 6, requests-per-second says 8 -> 8 wins
    assert hpa.sync(0, 4, [(90, 60), (200, 100)]) == 8

    # load drops: raw recommendation is 2, but the window remembers 8
    replicas = 8
    for t in range(15, 300, 15):
        replicas = hpa.sync(t, replicas, [(15, 60)])
        assert replicas == 8, (t, replicas)
    # 300 s after the peak recommendation it is allowed to scale down
    replicas = hpa.sync(300, replicas, [(15, 60)])
    assert replicas == 2, replicas

    # spike from 2 pods to a raw demand of 20: capped at +4 per period
    hpa2 = HPA(min_replicas=2, max_replicas=50)
    assert hpa2.sync(0, 2, [(600, 60)]) == 6
    assert hpa2.sync(15, 6, [(200, 60)]) == 12
    print("hpa ok")
```

The cap explains a common surprise: a sudden 10x spike takes several sync periods to absorb, and each new pod also needs time to be scheduled, pull its image and pass readiness. Autoscaling handles ramps; for step changes you need headroom (a higher `minReplicas` or a lower utilization target) or load shedding.

### Metrics sources

| Metric type | Source API | Example |
|---|---|---|
| `Resource` | `metrics.k8s.io` (metrics-server) | CPU or memory utilization of the pods |
| `Pods` | `custom.metrics.k8s.io` | Requests per second per pod, averaged |
| `Object` | `custom.metrics.k8s.io` | Requests per second on an Ingress |
| `External` | `external.metrics.k8s.io` | SQS queue depth, Kafka consumer lag |

metrics-server provides only CPU and memory. Custom and external metrics need an adapter that serves those APIs from your monitoring system, such as Prometheus Adapter, a cloud provider adapter, or KEDA.

### Custom metrics

CPU is a good scaling signal only for CPU-bound work. A Django API that spends most of its time waiting on Postgres and HTTP calls can be saturated (all Gunicorn workers busy, requests queueing) at 30% CPU. Better signals:

- **In-flight requests or worker busy ratio per pod**: the direct measure of saturation for a fixed worker pool.
- **Requests per second per pod**, with the target set from a load test of what one pod sustains at your latency objective (see [load and stress testing]({{ '/backend/load-and-stress-testing/' | relative_url }})).
- **Queue depth or consumer lag** for workers: scale on backlog divided by per-pod throughput.

Memory is usually a poor autoscaling metric: Python processes rarely give memory back, so the HPA scales up and never down.

### KEDA

KEDA (Kubernetes Event-driven Autoscaling, a CNCF graduated project) is the standard way to autoscale consumers. You declare a `ScaledObject` with triggers (Kafka lag, RabbitMQ or SQS queue length, Redis list length, a Prometheus query, cron schedules and dozens more); KEDA serves those as external metrics and creates and manages an HPA for you. It also handles **scale to zero**: the plain HPA can't go below one replica without an alpha feature gate, while KEDA deactivates the workload when the trigger is idle and activates it on the first message. `ScaledJob` runs one Kubernetes Job per batch of messages, which suits long, uneven tasks.

Neighbors: the **Vertical Pod Autoscaler** right-sizes requests (don't let it and an HPA both act on CPU for the same workload), and the **cluster autoscaler** or Karpenter adds nodes when pods are `Pending`. Pod autoscaling without node autoscaling just produces `Pending` pods.

## In practice

Illustrative manifests for a Django API: no `replicas` field (the HPA owns it), zero-unavailable rollout, zone spreading, probes, requests and a hardened security context.

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders-api
spec:
  revisionHistoryLimit: 5
  strategy:
    type: RollingUpdate
    rollingUpdate: {maxSurge: 25%, maxUnavailable: 0}
  selector:
    matchLabels: {app: orders-api}
  template:
    metadata:
      labels: {app: orders-api}
    spec:
      terminationGracePeriodSeconds: 40
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels: {app: orders-api}
      securityContext:
        runAsNonRoot: true
        runAsUser: 10001
      containers:
        - name: api
          image: ghcr.io/acme/orders-api@sha256:4b1c0e...
          ports: [{containerPort: 8000}]
          envFrom:
            - configMapRef: {name: orders-api-config}
            - secretRef: {name: orders-api-secrets}
          resources:
            requests: {cpu: 500m, memory: 768Mi}
            limits: {memory: 768Mi}
          startupProbe:
            httpGet: {path: /healthz, port: 8000}
            periodSeconds: 2
            failureThreshold: 30
          readinessProbe:
            httpGet: {path: /readyz, port: 8000}
            periodSeconds: 5
            failureThreshold: 2
          livenessProbe:
            httpGet: {path: /healthz, port: 8000}
            periodSeconds: 10
            failureThreshold: 3
          lifecycle:
            preStop:
              exec: {command: ["sleep", "5"]}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - name: tmp
              mountPath: /tmp
      volumes:
        - name: tmp
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: orders-api
spec:
  selector: {app: orders-api}
  ports: [{port: 80, targetPort: 8000}]
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: orders-api
spec:
  ingressClassName: nginx
  tls: [{hosts: [api.example.com], secretName: api-tls}]
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /orders
            pathType: Prefix
            backend:
              service:
                name: orders-api
                port:
                  number: 80
---
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: orders-api
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: orders-api
  minReplicas: 3
  maxReplicas: 30
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: Utilization
          averageUtilization: 60
    - type: Pods
      pods:
        metric:
          name: http_requests_in_flight
        target:
          type: AverageValue
          averageValue: "6"
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 300
      policies:
        - type: Percent
          value: 20
          periodSeconds: 60
```

A KEDA `ScaledObject` for Celery workers consuming a Redis list, scaling to zero when idle (illustrative):

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: celery-worker
spec:
  scaleTargetRef:
    name: celery-worker
  minReplicaCount: 0
  maxReplicaCount: 40
  cooldownPeriod: 300
  triggers:
    - type: redis
      metadata:
        address: redis.infra.svc.cluster.local:6379
        listName: celery
        listLength: "50"          # target backlog per replica
```

Add a PodDisruptionBudget (`maxUnavailable: 1`) next to these so node drains never take out more than one replica. Day-to-day commands:

```bash
kubectl rollout undo deploy/orders-api          # back to previous ReplicaSet
kubectl get hpa orders-api --watch              # current vs target metrics
kubectl describe pod orders-api-7f9c6-abcde     # FailedScheduling, OOMKilled, probes
kubectl top pods -l app=orders-api              # usage vs requests
```

## Pitfalls

- **No requests.** Pods are BestEffort, get evicted first, and the HPA can't compute CPU utilization at all (it reports `<unknown>`). Fix: set requests from observed p95 usage.
- **Liveness probe checks the database.** Symptom: a short Postgres failover restarts the whole fleet. Fix: liveness checks only the process.
- **CPU limits on latency-sensitive services.** Symptom: p99 spikes while average CPU looks fine; `container_cpu_cfs_throttled_periods_total` climbs. Fix: raise or remove the CPU limit, keep the request.
- **`replicas` in Git plus an HPA.** Every GitOps sync resets the count, fighting the autoscaler. Fix: omit `replicas` from the manifest when an HPA manages it.
- **Autoscaling into the database.** More pods, more connections, Postgres saturates, latency rises, HPA adds pods. Fix: cap `maxReplicas`, pool connections with PgBouncer (see [connection pooling]({{ '/backend/connection-pooling/' | relative_url }})).
- **Single replica or no PDB.** A routine node drain becomes downtime. Fix: at least 2–3 replicas spread across zones and a PodDisruptionBudget.

## Interview questions

**What happens when you `kubectl apply` a Deployment?** The API server validates and stores it in etcd. The Deployment controller creates or updates a ReplicaSet, which creates pods. The scheduler assigns each pod to a node by requests and constraints, the kubelet on that node pulls the image and starts containers, and once readiness passes the pod's IP joins the Service's EndpointSlices.

**Requests vs limits?** Requests are reservations used for scheduling and CPU sharing; limits are caps. Over the memory limit the container is OOM-killed; over the CPU limit it is throttled. Requests equal to limits gives Guaranteed QoS.

**Liveness vs readiness vs startup probes?** Startup gates the other two during slow boots. Readiness controls whether the pod receives traffic. Liveness restarts a wedged container. Liveness should never depend on external systems.

**How does the HPA decide the replica count?** `ceil(current * currentMetric / target)` per metric, take the max across metrics, skip changes within a 10% tolerance, use the highest recommendation of the last 5 minutes before scaling down, rate-limit scale-up, and clamp to min and max.

**Why might the HPA not scale a Celery worker deployment correctly on CPU?** Workers are often I/O bound and the backlog grows while CPU stays low. Scale on queue length or lag per replica with KEDA or an external metric, and allow scale to zero for bursty queues.

**How does Kubernetes heal from a node failure?** The node controller marks the node unreachable, pods on it are evicted after the toleration period (5 minutes by default), and ReplicaSets create replacements on healthy nodes. Spread replicas across zones so capacity survives meanwhile.

**Kubernetes vs ECS?** Kubernetes offers a portable, extensible API and ecosystem at the cost of complexity; ECS is simpler and deeply integrated with AWS but locks you in. Choose by team size, number of services and whether you need the ecosystem.

## Further reading

- Kubernetes documentation: Concepts (Workloads, Services, Configuration) and Horizontal Pod Autoscaling.
- *Large-scale cluster management at Google with Borg*, Verma et al. (EuroSys 2015).
- *Borg, Omega, and Kubernetes*, Burns, Grant, Oppenheimer, Brewer and Wilkes (ACM Queue, 2016).
- *Kubernetes: Up and Running*, Brendan Burns, Joe Beda, Kelsey Hightower and Lachlan Evenson (O'Reilly, 3rd edition).
- KEDA documentation: Concepts and Scalers.
