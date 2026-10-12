"""The web backend: the only public face of Pinboard. It runs on Cloud Run behind IAP, checks who is signed in, and relays to the engine
(Agent Runtime) with its own credentials. The browser never talks to the engine and never supplies a user id."""
