"""Conservative spatial broad phase for immutable regional copper primitives."""
from collections import defaultdict
import math

class PrimitiveIndex:
    def __init__(self, entries, predicate, bucket_mm=1.0):
        self.entries=list(entries);self.predicate=predicate;self.bucket_mm=bucket_mm
        if not bucket_mm>0:raise ValueError('positive bucket required')
        self.buckets=defaultdict(list)
        self.max_half_width=max((.3 if p[0]=='via' else r.width/2 for r,p in self.entries),default=0.)
        self.max_clearance=max((r.clearance for r,p in self.entries),default=0.)
        for index,(_,p) in enumerate(self.entries):
            for cell in self.cells(p[2],p[3],0):self.buckets[cell].append(index)

    def cells(self,a,b,gap):
        size=self.bucket_mm
        for x in range(math.floor((min(a[0],b[0])-gap)/size),math.floor((max(a[0],b[0])+gap)/size)+1):
            for y in range(math.floor((min(a[1],b[1])-gap)/size),math.floor((max(a[1],b[1])+gap)/size)+1):
                yield x,y

    def collides(self,request,primitive):
        gap=(.3 if primitive[0]=='via' else request.width/2)+self.max_half_width+max(request.clearance,self.max_clearance)
        seen=set()
        for cell in self.cells(primitive[2],primitive[3],gap):
            for index in self.buckets.get(cell,()):
                if index in seen:continue
                seen.add(index)
                other,copper=self.entries[index]
                if self.predicate(request,primitive,other,copper):return True
        return False
