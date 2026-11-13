import os
_p=os.path.join(os.path.dirname(__file__),'.env')
for line in open(_p):
    line=line.strip()
    if line and '=' in line: k,v=line.split('=',1); os.environ[k]=v
