# claude-blender

A Blender add-on that lets Claude work on your scene. It gives Claude about 35 tools
(inspecting the scene, modeling, procedural assets, materials, lighting, cameras,
animation, rendering) and adds a chat panel to the 3D viewport sidebar.

## How it works

The add-on runs a small server inside Blender on `localhost:9877`. `mcp_server.py` is an
MCP server that passes Claude's tool calls to it. The chat panel runs Claude Code in the
background with that MCP server attached.

## Install

You need Blender 4.2+, [Claude Code](https://claude.com/claude-code) and [uv](https://docs.astral.sh/uv/).

1. Download `claude_blender-1.0.0.zip` from [Releases](https://github.com/atharv-santosh/claude-blender/releases).
2. In Blender: Edit > Preferences > Get Extensions > ⌄ > Install from Disk, and pick the zip.
3. Open the sidebar (N) > Claude tab > Connection & Settings > Connect Claude Code.

## Use

- Type in the Claude tab and press Enter, or
- run `claude` in a terminal and ask it to do something in Blender.

Every change Claude makes is a single undo step.
