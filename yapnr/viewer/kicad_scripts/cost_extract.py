import json,sys,hashlib
from pathlib import Path
import pcbnew as k
import wx
app=wx.App(False)
from pnr.ingest import build_graph
board=Path(sys.argv[1]);assert hashlib.sha256(board.read_bytes()).hexdigest()==sys.argv[3],'Board changed'
b=k.LoadBoard(str(board));Path(sys.argv[2]).write_text(build_graph(b).to_json())
