# Nemesis

This is a PoC of a Python3 framework that is designed to implement cloud native applications that run in Kubernetes. 

Nemesis focuses on what Kubernetes supports and provides for application developers and 
tries to enable those capabilities without headache.

Features to Support:

- TCP/UDP transport protocols
- HTTP1/1, SSL/HTTPS2, Websocket app-level protocols
- TBA

## Architecture

Nemesis respects [ASGI's](https://asgi.readthedocs.io/en/latest/introduction.html#introduction) approach to building 
network-based applications and consists of similar two parts:

- Protocol Server (Low-level network server piece that supports Kubernetes capabilities)
- App Framework Integrations (Starlette and FastAPI are the main targets)

## Credits

This project is staying on the shoulders of giants:

- https://pgjones.gitlab.io/hypercorn/
- https://www.starlette.io/
- TBA

