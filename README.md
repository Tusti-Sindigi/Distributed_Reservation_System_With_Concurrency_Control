# Distributed Reservation System with Concurrency Control
### Secure Socket Programming

## 1. Project Overview
This project implements a secure, multi-client networked reservation system designed to manage shared resources (e.g., seats, rooms, or slots). 
The system ensures *atomic and consistent booking* under high concurrency by utilizing low-level *TCP* sockets and *fine-grained locking mechanisms*.

### Core Objectives:
i)   Design a custom application-layer protocol for resource management
ii)  Ensure thread-safe operations to prevent double-booking
iii) Implement mandatory $SSL/TLS$ encryption for all network traffic
iv)  Evaluate system performance under heavy concurrent loads

---

## 2. System Architecture
The system follows a *Multi-Threaded Client-Server Architecture*. All communication occurs over secure *TCP sockets*, strictly avoiding local IPC or shared memory.



### Components:
i)   *Secure Server:* A multi-threaded Python server that manages the state of resources and enforces concurrency rules.
ii)  *Secure Client:* A terminal-based interface allowing users to view, book, and cancel reservations securely.
iii) *Concurrency Manager:* A fine-grained locking system (Lock-per-Resource) that maximizes throughput while maintaining data integrity.

---

## 3. Custom Protocol Design
To meet the requirement for a custom request-response protocol, we utilize a structured string-based format:

i)   **VIEW** - Requests the current status of all resources
              - Server response --> JSON-formatted string of resource states
ii)  **BOOK <id>** - Attempts to reserve a specific resource
                   - Server response --> *SUCCESS: Reserved* or *FAIL: Taken*
iii) **CANCEL <id>** - Releases a resource owned by the user
                     - Server response --> *SUCCESS: Released* or *FAIL: Not Owner*
iv)  **EXIT** - Gracefully terminates the connection
              - Server response --> Connection Close

---

## 4. Implementation Details

### Security using SSL/TLS:
Mandatory encryption is implemented using Python’s *ssl* module
--> *Server-side:* Loads a self-signed certificate *server.crt* and *private key*
--> *Client-side:* Wraps the standard *TCS socket* in a secure context before the handshake

### Concurrency Control:
i)  *Fine-Grained Locking:* Instead of a global lock, each resource has its own *threading.Lock*. 
                            This allows concurrent bookings for different seats without blocking the entire server.
ii) *Atomic Updates:* The "Check-then-Act" sequence is wrapped in a context manager to prevent race conditions.
