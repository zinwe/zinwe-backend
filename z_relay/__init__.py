"""
v1.5 round 2 file relay: the bytes of a chat file stream from the sender's device to the receiver through this
process, in memory only, never written to disk. `z_relay.app` is a plain ASGI app run as its own process
(`uvicorn z_relay.app:app --workers 1 --port <port>`); `z_relay.tickets` signs and checks the tickets Django hands out.
Neither touches the database. Not an installed Django app on purpose.
"""
