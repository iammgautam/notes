---
layout: section
section: backend
title: Backend System Design
---
The full backend system design roadmap for senior and SDE-2/SDE-3 interviews: 150 topics in 10 areas, from architecture styles and databases to messaging, security, operations and 15 end-to-end designs. Related topics share a note so each one goes deep; the table below maps every roadmap topic to the note that covers it.

<details markdown="1">
<summary>All 150 roadmap topics and where each is covered</summary>

| Area | Topic | Note |
|---|---|---|
| Backend architecture | Monolithic Architecture | [monolith and modular monolith]({{ '/backend/monolith-and-modular-monolith/' | relative_url }}) |
| Backend architecture | Microservices Architecture | [microservices and soa]({{ '/backend/microservices-and-soa/' | relative_url }}) |
| Backend architecture | Service-Oriented Architecture (SOA) | [microservices and soa]({{ '/backend/microservices-and-soa/' | relative_url }}) |
| Backend architecture | Event-Driven Architecture | [event driven architecture]({{ '/backend/event-driven-architecture/' | relative_url }}) |
| Backend architecture | Layered Architecture | [layered clean hexagonal]({{ '/backend/layered-clean-hexagonal/' | relative_url }}) |
| Backend architecture | Clean Architecture | [layered clean hexagonal]({{ '/backend/layered-clean-hexagonal/' | relative_url }}) |
| Backend architecture | Domain-Driven Design (DDD) | [domain driven design]({{ '/backend/domain-driven-design/' | relative_url }}) |
| Backend architecture | Hexagonal Architecture | [layered clean hexagonal]({{ '/backend/layered-clean-hexagonal/' | relative_url }}) |
| Backend architecture | API Gateway Pattern | [api gateway and bff]({{ '/backend/api-gateway-and-bff/' | relative_url }}) |
| Backend architecture | Backend for Frontend (BFF) | [api gateway and bff]({{ '/backend/api-gateway-and-bff/' | relative_url }}) |
| Backend architecture | Service Discovery | [service discovery and mesh]({{ '/backend/service-discovery-and-mesh/' | relative_url }}) |
| Backend architecture | Service Mesh | [service discovery and mesh]({{ '/backend/service-discovery-and-mesh/' | relative_url }}) |
| Backend architecture | Multi-Tenant Architecture | [multi tenant architecture]({{ '/backend/multi-tenant-architecture/' | relative_url }}) |
| Backend architecture | Modular Monolith | [monolith and modular monolith]({{ '/backend/monolith-and-modular-monolith/' | relative_url }}) |
| Backend architecture | Distributed System Fundamentals | [distributed systems fundamentals]({{ '/backend/distributed-systems-fundamentals/' | relative_url }}) |
| Databases | SQL vs NoSQL | [sql vs nosql]({{ '/backend/sql-vs-nosql/' | relative_url }}) |
| Databases | Relational Database Design | [relational design and normalization]({{ '/backend/relational-design-and-normalization/' | relative_url }}) |
| Databases | Database Normalization | [relational design and normalization]({{ '/backend/relational-design-and-normalization/' | relative_url }}) |
| Databases | Database Denormalization | [relational design and normalization]({{ '/backend/relational-design-and-normalization/' | relative_url }}) |
| Databases | Indexing Strategies | [indexing strategies]({{ '/backend/indexing-strategies/' | relative_url }}) |
| Databases | Composite Indexes | [indexing strategies]({{ '/backend/indexing-strategies/' | relative_url }}) |
| Databases | Database Partitioning | [partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}) |
| Databases | Sharding | [partitioning and sharding]({{ '/backend/partitioning-and-sharding/' | relative_url }}) |
| Databases | Replication | [replication]({{ '/backend/replication/' | relative_url }}) |
| Databases | Master-Slave Architecture | [replication]({{ '/backend/replication/' | relative_url }}) |
| Databases | ACID Properties | [acid and transactions]({{ '/backend/acid-and-transactions/' | relative_url }}) |
| Databases | CAP Theorem | [cap and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) |
| Databases | Database Transactions | [acid and transactions]({{ '/backend/acid-and-transactions/' | relative_url }}) |
| Databases | Connection Pooling | [connection pooling]({{ '/backend/connection-pooling/' | relative_url }}) |
| Databases | Database Migration Strategies | [database migrations]({{ '/backend/database-migrations/' | relative_url }}) |
| API design | REST API Design | [rest api design]({{ '/backend/rest-api-design/' | relative_url }}) |
| API design | GraphQL | [graphql]({{ '/backend/graphql/' | relative_url }}) |
| API design | gRPC | [grpc and soap]({{ '/backend/grpc-and-soap/' | relative_url }}) |
| API design | SOAP | [grpc and soap]({{ '/backend/grpc-and-soap/' | relative_url }}) |
| API design | API Versioning | [api versioning and documentation]({{ '/backend/api-versioning-and-documentation/' | relative_url }}) |
| API design | API Gateway | [api gateway and bff]({{ '/backend/api-gateway-and-bff/' | relative_url }}) |
| API design | Rate Limiting | [rate limiting]({{ '/backend/rate-limiting/' | relative_url }}) |
| API design | Pagination Strategies | [pagination]({{ '/backend/pagination/' | relative_url }}) |
| API design | Idempotency | [idempotency]({{ '/backend/idempotency/' | relative_url }}) |
| API design | Request Validation | [validation and error handling]({{ '/backend/validation-and-error-handling/' | relative_url }}) |
| API design | Error Handling & Status Codes | [validation and error handling]({{ '/backend/validation-and-error-handling/' | relative_url }}) |
| API design | WebSockets | [real time communication]({{ '/backend/real-time-communication/' | relative_url }}) |
| API design | Server-Sent Events (SSE) | [real time communication]({{ '/backend/real-time-communication/' | relative_url }}) |
| API design | Long Polling | [real time communication]({{ '/backend/real-time-communication/' | relative_url }}) |
| API design | API Documentation (OpenAPI/Swagger) | [api versioning and documentation]({{ '/backend/api-versioning-and-documentation/' | relative_url }}) |
| Caching | Caching Fundamentals | [caching fundamentals]({{ '/backend/caching-fundamentals/' | relative_url }}) |
| Caching | Client-Side vs Server-Side Caching | [caching fundamentals]({{ '/backend/caching-fundamentals/' | relative_url }}) |
| Caching | In-Memory Caching | [caching fundamentals]({{ '/backend/caching-fundamentals/' | relative_url }}) |
| Caching | Redis | [redis and memcached]({{ '/backend/redis-and-memcached/' | relative_url }}) |
| Caching | Memcached | [redis and memcached]({{ '/backend/redis-and-memcached/' | relative_url }}) |
| Caching | Cache-Aside Pattern | [caching patterns]({{ '/backend/caching-patterns/' | relative_url }}) |
| Caching | Read-Through Cache | [caching patterns]({{ '/backend/caching-patterns/' | relative_url }}) |
| Caching | Write-Through Cache | [caching patterns]({{ '/backend/caching-patterns/' | relative_url }}) |
| Caching | Write-Behind Cache | [caching patterns]({{ '/backend/caching-patterns/' | relative_url }}) |
| Caching | Cache Invalidation | [cache invalidation and eviction]({{ '/backend/cache-invalidation-and-eviction/' | relative_url }}) |
| Caching | TTL & Eviction Policies | [cache invalidation and eviction]({{ '/backend/cache-invalidation-and-eviction/' | relative_url }}) |
| Caching | Distributed Caching | [distributed caching and cdn]({{ '/backend/distributed-caching-and-cdn/' | relative_url }}) |
| Caching | CDN Caching | [distributed caching and cdn]({{ '/backend/distributed-caching-and-cdn/' | relative_url }}) |
| Caching | Cache Stampede | [cache stampede]({{ '/backend/cache-stampede/' | relative_url }}) |
| Caching | Cache Consistency | [cache invalidation and eviction]({{ '/backend/cache-invalidation-and-eviction/' | relative_url }}) |
| Distributed systems | Horizontal vs Vertical Scaling | [scaling and load balancing]({{ '/backend/scaling-and-load-balancing/' | relative_url }}) |
| Distributed systems | Load Balancing | [scaling and load balancing]({{ '/backend/scaling-and-load-balancing/' | relative_url }}) |
| Distributed systems | Consistent Hashing | [consistent hashing]({{ '/backend/consistent-hashing/' | relative_url }}) |
| Distributed systems | CAP Theorem | [cap and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) |
| Distributed systems | Eventual Consistency | [cap and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) |
| Distributed systems | Strong Consistency | [cap and consistency models]({{ '/backend/cap-and-consistency-models/' | relative_url }}) |
| Distributed systems | Distributed Transactions | [distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }}) |
| Distributed systems | Two-Phase Commit (2PC) | [distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }}) |
| Distributed systems | Saga Pattern | [distributed transactions]({{ '/backend/distributed-transactions/' | relative_url }}) |
| Distributed systems | Leader Election | [leader election and consensus]({{ '/backend/leader-election-and-consensus/' | relative_url }}) |
| Distributed systems | Distributed Locking | [distributed locking]({{ '/backend/distributed-locking/' | relative_url }}) |
| Distributed systems | Fault Tolerance | [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}) |
| Distributed systems | Circuit Breaker Pattern | [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}) |
| Distributed systems | Bulkhead Pattern | [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}) |
| Distributed systems | Retry & Backoff Strategies | [retries and backoff]({{ '/backend/retries-and-backoff/' | relative_url }}) |
| Messaging and events | Message Queue Fundamentals | [message queue fundamentals]({{ '/backend/message-queue-fundamentals/' | relative_url }}) |
| Messaging and events | Apache Kafka | [kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }}) |
| Messaging and events | RabbitMQ | [rabbitmq and sqs]({{ '/backend/rabbitmq-and-sqs/' | relative_url }}) |
| Messaging and events | AWS SQS | [rabbitmq and sqs]({{ '/backend/rabbitmq-and-sqs/' | relative_url }}) |
| Messaging and events | Publish-Subscribe Pattern | [message queue fundamentals]({{ '/backend/message-queue-fundamentals/' | relative_url }}) |
| Messaging and events | Event Streaming | [kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }}) |
| Messaging and events | Event Sourcing | [event sourcing and cqrs]({{ '/backend/event-sourcing-and-cqrs/' | relative_url }}) |
| Messaging and events | CQRS | [event sourcing and cqrs]({{ '/backend/event-sourcing-and-cqrs/' | relative_url }}) |
| Messaging and events | Dead Letter Queues (DLQ) | [ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }}) |
| Messaging and events | Message Ordering | [ordering and dead letter queues]({{ '/backend/ordering-and-dead-letter-queues/' | relative_url }}) |
| Messaging and events | At-Least-Once Delivery | [delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}) |
| Messaging and events | At-Most-Once Delivery | [delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}) |
| Messaging and events | Exactly-Once Processing | [delivery semantics]({{ '/backend/delivery-semantics/' | relative_url }}) |
| Messaging and events | Consumer Groups | [kafka and event streaming]({{ '/backend/kafka-and-event-streaming/' | relative_url }}) |
| Messaging and events | Backpressure Handling | [backpressure]({{ '/backend/backpressure/' | relative_url }}) |
| Security | Authentication vs Authorization | [authentication and sessions]({{ '/backend/authentication-and-sessions/' | relative_url }}) |
| Security | Session-Based Authentication | [authentication and sessions]({{ '/backend/authentication-and-sessions/' | relative_url }}) |
| Security | JWT Authentication | [jwt and refresh tokens]({{ '/backend/jwt-and-refresh-tokens/' | relative_url }}) |
| Security | OAuth 2.0 | [oauth2 and oidc]({{ '/backend/oauth2-and-oidc/' | relative_url }}) |
| Security | OpenID Connect | [oauth2 and oidc]({{ '/backend/oauth2-and-oidc/' | relative_url }}) |
| Security | Role-Based Access Control (RBAC) | [rbac and abac]({{ '/backend/rbac-and-abac/' | relative_url }}) |
| Security | Attribute-Based Access Control (ABAC) | [rbac and abac]({{ '/backend/rbac-and-abac/' | relative_url }}) |
| Security | Password Hashing (bcrypt/Argon2) | [password hashing]({{ '/backend/password-hashing/' | relative_url }}) |
| Security | Refresh Token Rotation | [jwt and refresh tokens]({{ '/backend/jwt-and-refresh-tokens/' | relative_url }}) |
| Security | API Key Management | [api keys and secrets]({{ '/backend/api-keys-and-secrets/' | relative_url }}) |
| Security | Encryption at Rest & In Transit | [encryption]({{ '/backend/encryption/' | relative_url }}) |
| Security | SQL Injection | [web vulnerabilities]({{ '/backend/web-vulnerabilities/' | relative_url }}) |
| Security | Cross-Site Scripting (XSS) | [web vulnerabilities]({{ '/backend/web-vulnerabilities/' | relative_url }}) |
| Security | CSRF Protection | [web vulnerabilities]({{ '/backend/web-vulnerabilities/' | relative_url }}) |
| Security | Secrets Management | [api keys and secrets]({{ '/backend/api-keys-and-secrets/' | relative_url }}) |
| Performance and reliability | Database Query Optimization | [query optimization and n plus one]({{ '/backend/query-optimization-and-n-plus-one/' | relative_url }}) |
| Performance and reliability | N+1 Query Problem | [query optimization and n plus one]({{ '/backend/query-optimization-and-n-plus-one/' | relative_url }}) |
| Performance and reliability | Connection Pooling | [connection pooling]({{ '/backend/connection-pooling/' | relative_url }}) |
| Performance and reliability | Asynchronous Processing | [async processing and thread pools]({{ '/backend/async-processing-and-thread-pools/' | relative_url }}) |
| Performance and reliability | Thread Pools | [async processing and thread pools]({{ '/backend/async-processing-and-thread-pools/' | relative_url }}) |
| Performance and reliability | Non-Blocking I/O | [non blocking io]({{ '/backend/non-blocking-io/' | relative_url }}) |
| Performance and reliability | Memory Management | [memory and cpu profiling]({{ '/backend/memory-and-cpu-profiling/' | relative_url }}) |
| Performance and reliability | CPU Profiling | [memory and cpu profiling]({{ '/backend/memory-and-cpu-profiling/' | relative_url }}) |
| Performance and reliability | Load Testing | [load and stress testing]({{ '/backend/load-and-stress-testing/' | relative_url }}) |
| Performance and reliability | Stress Testing | [load and stress testing]({{ '/backend/load-and-stress-testing/' | relative_url }}) |
| Performance and reliability | Rate Limiting & Throttling | [rate limiting]({{ '/backend/rate-limiting/' | relative_url }}) |
| Performance and reliability | Graceful Shutdown | [graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}) |
| Performance and reliability | Health Checks | [graceful shutdown and health checks]({{ '/backend/graceful-shutdown-and-health-checks/' | relative_url }}) |
| Performance and reliability | Fault Isolation | [fault tolerance patterns]({{ '/backend/fault-tolerance-patterns/' | relative_url }}) |
| Performance and reliability | Performance Monitoring | [performance monitoring]({{ '/backend/performance-monitoring/' | relative_url }}) |
| DevOps and observability | CI/CD Pipelines | [ci cd pipelines]({{ '/backend/ci-cd-pipelines/' | relative_url }}) |
| DevOps and observability | Docker & Containerization | [docker and containers]({{ '/backend/docker-and-containers/' | relative_url }}) |
| DevOps and observability | Kubernetes | [kubernetes]({{ '/backend/kubernetes/' | relative_url }}) |
| DevOps and observability | Container Orchestration | [kubernetes]({{ '/backend/kubernetes/' | relative_url }}) |
| DevOps and observability | Blue-Green Deployment | [deployment strategies]({{ '/backend/deployment-strategies/' | relative_url }}) |
| DevOps and observability | Canary Deployment | [deployment strategies]({{ '/backend/deployment-strategies/' | relative_url }}) |
| DevOps and observability | Rolling Deployments | [deployment strategies]({{ '/backend/deployment-strategies/' | relative_url }}) |
| DevOps and observability | Infrastructure as Code | [infrastructure as code]({{ '/backend/infrastructure-as-code/' | relative_url }}) |
| DevOps and observability | Horizontal Pod Autoscaling | [kubernetes]({{ '/backend/kubernetes/' | relative_url }}) |
| DevOps and observability | Centralized Logging | [logging and metrics]({{ '/backend/logging-and-metrics/' | relative_url }}) |
| DevOps and observability | Metrics & Monitoring | [logging and metrics]({{ '/backend/logging-and-metrics/' | relative_url }}) |
| DevOps and observability | Distributed Tracing | [distributed tracing and opentelemetry]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}) |
| DevOps and observability | OpenTelemetry | [distributed tracing and opentelemetry]({{ '/backend/distributed-tracing-and-opentelemetry/' | relative_url }}) |
| DevOps and observability | Alerting & Incident Management | [alerting and incidents]({{ '/backend/alerting-and-incidents/' | relative_url }}) |
| DevOps and observability | Disaster Recovery & Backup Strategies | [disaster recovery and backups]({{ '/backend/disaster-recovery-and-backups/' | relative_url }}) |
| System design case studies | Design URL Shortener (Bitly) | [design url shortener]({{ '/backend/design-url-shortener/' | relative_url }}) |
| System design case studies | Design WhatsApp Backend | [design whatsapp]({{ '/backend/design-whatsapp/' | relative_url }}) |
| System design case studies | Design Instagram Backend | [design instagram]({{ '/backend/design-instagram/' | relative_url }}) |
| System design case studies | Design YouTube Backend | [design youtube]({{ '/backend/design-youtube/' | relative_url }}) |
| System design case studies | Design Netflix Backend | [design netflix]({{ '/backend/design-netflix/' | relative_url }}) |
| System design case studies | Design Uber Backend | [design uber]({{ '/backend/design-uber/' | relative_url }}) |
| System design case studies | Design Amazon E-commerce Backend | [design ecommerce]({{ '/backend/design-ecommerce/' | relative_url }}) |
| System design case studies | Design Twitter/X Backend | [design twitter]({{ '/backend/design-twitter/' | relative_url }}) |
| System design case studies | Design Notification Service | [design notification service]({{ '/backend/design-notification-service/' | relative_url }}) |
| System design case studies | Design Payment System | [design payment system]({{ '/backend/design-payment-system/' | relative_url }}) |
| System design case studies | Design Rate Limiter | [design rate limiter]({{ '/backend/design-rate-limiter/' | relative_url }}) |
| System design case studies | Design Distributed Job Scheduler | [design job scheduler]({{ '/backend/design-job-scheduler/' | relative_url }}) |
| System design case studies | Design File Storage System (Google Drive) | [design google drive]({{ '/backend/design-google-drive/' | relative_url }}) |
| System design case studies | Design Search Engine | [design search engine]({{ '/backend/design-search-engine/' | relative_url }}) |
| System design case studies | Design Real-Time Chat System | [design chat system]({{ '/backend/design-chat-system/' | relative_url }}) |

</details>

Start with [the design framework]({{ '/backend/design-framework/' | relative_url }}) if you have an interview soon; otherwise go in order.
