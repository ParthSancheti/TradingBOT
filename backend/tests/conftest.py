import copy
from datetime import datetime, timezone
from jarvis.sheets import SheetsError


class FakeSheets:
    """Explicit test double only. Never selected by the application."""
    def __init__(self): self.records={};self.writes=0
    async def get(self,c,i): return copy.deepcopy(self.records.get((c,i)))
    async def list(self,c,limit=100): return [copy.deepcopy(v) for (name,_),v in self.records.items() if name==c][:limit]
    async def put(self,c,i,data,revision,request_id=None):
        old=self.records.get((c,i))
        if (old["revision"] if old else 0)!=revision: raise SheetsError("REVISION_CONFLICT")
        record={"id":i,"revision":revision+1,"data":copy.deepcopy(data),"updated_at":datetime.now(timezone.utc).isoformat()}
        self.records[c,i]=record;self.writes+=1
        return copy.deepcopy(record)
    async def health(self): return {"schema_version":1,"storage":"google_sheets"}


class FakeVault:
    def __init__(self): self.values={}
    def get(self,k): return self.values.get(k)
    def set(self,k,v): self.values[k]=v
