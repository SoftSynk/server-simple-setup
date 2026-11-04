"""Shared library for the softsynk Python tools (standard library only).

core: paths, config, files, processes, locks, logs, status.  sysinfo: host facts from /proc and systemd.
ui: prompts and output.  change: backup, apply, verify and roll back config edits.
Scripts import it with: sys.path.insert(0, <their realpath>/../../lib); sys.dont_write_bytecode = True.
"""
