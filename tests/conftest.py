"""Tests live in folders by area (core, state, engine, agent, gcp, web, deploy). Some share fixtures by importing from each other's files
(`from test_policy import BASE`), so every test folder is put on the import path here: new folders work without any change."""
import sys
from pathlib import Path

for folder in sorted(Path(__file__).parent.iterdir()):
    if folder.is_dir() and not folder.name.startswith(("_", ".")):
        sys.path.insert(0, str(folder))
