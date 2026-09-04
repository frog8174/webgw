# Regenerate the README demo:
#
#   docker run --rm -v "$PWD:/vhs" \
#     -e WEBGW_URL=https://your-webgw \
#     -e WEBGW_TOKEN=your-token \
#     ghcr.io/charmbracelet/vhs demo/demo.tape
#
# The token comes from the environment, never from this file -- this tape is
# committed. demo.py needs no dependencies, so the recorder image needs no
# setup beyond the python3 it already ships.

Output demo/demo.gif

Set Shell bash
Set FontSize 15
Set Width 1120
Set Height 400
Set Padding 24
Set Theme "Catppuccin Mocha"
Set TypingSpeed 40ms

Hide
Type "cd /vhs && clear"
Enter
Show

Sleep 700ms
Type "python3 demo/demo.py"
Sleep 500ms
Enter

# bm25 ~1s, rerank ~6s, plus the MCP handshake.
Sleep 16s
