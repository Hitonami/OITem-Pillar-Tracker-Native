import math
SQ_RECT='<rect x="100" y="100" width="824" height="824" rx="186"/>'
TIP=(575,455); REST=(290,740); R=118; RR=R+62
defs='''
<clipPath id="sq">'''+SQ_RECT+'''</clipPath>
<filter id="drop" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="12" stdDeviation="14" flood-color="#000" flood-opacity="0.28"/></filter>
<linearGradient id="bg" x1="0.15" y1="0" x2="0.85" y2="1"><stop offset="0" stop-color="#38bdf8"/><stop offset="0.5" stop-color="#2563eb"/><stop offset="1" stop-color="#4338ca"/></linearGradient>
<radialGradient id="hi" cx="0.3" cy="0" r="0.85"><stop offset="0" stop-color="#fff" stop-opacity="0.32"/><stop offset="0.55" stop-color="#fff" stop-opacity="0"/></radialGradient>
<radialGradient id="tip" cx="0.38" cy="0.3" r="0.8"><stop offset="0" stop-color="#ffffff"/><stop offset="0.75" stop-color="#e3eeff"/><stop offset="1" stop-color="#a8c8ff"/></radialGradient>
<linearGradient id="ring" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#86efac"/><stop offset="1" stop-color="#16a34a"/></linearGradient>
<filter id="tipSh" x="-60%" y="-60%" width="220%" height="220%"><feDropShadow dx="0" dy="18" stdDeviation="20" flood-color="#1e1b4b" flood-opacity="0.5"/></filter>
<filter id="ringSh" x="-30%" y="-30%" width="160%" height="160%"><feDropShadow dx="0" dy="10" stdDeviation="12" flood-color="#052e16" flood-opacity="0.35"/></filter>
<linearGradient id="edge" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity="0.6"/><stop offset="0.5" stop-color="#fff" stop-opacity="0.08"/><stop offset="1" stop-color="#fff" stop-opacity="0.25"/></linearGradient>
'''
(x0,y0),(x1,y1)=REST,TIP
import math as _m
D=_m.hypot(x1-x0,y1-y0); ux,uy=(x1-x0)/D,(y1-y0)/D
start=70+30; end=D-RR-17-38
dots=[]
for i in range(3):
    d=start+(end-start)*i/2
    dots.append(f'<circle cx="{x0+ux*d:.1f}" cy="{y0+uy*d:.1f}" r="{13+6*i}" fill="#fde047" opacity="{0.5+0.22*i:.2f}"/>')
tx,ty=TIP
L=46
ticks=" ".join([f"M{tx} {ty-RR-L} V {ty-RR+8}", f"M{tx} {ty+RR-8} V {ty+RR+L}", f"M{tx-RR-L} {ty} H {tx-RR+8}", f"M{tx+RR-8} {ty} H {tx+RR+L}"])
body=f'''
<g filter="url(#drop)"><rect x="100" y="100" width="824" height="824" rx="186" fill="url(#bg)"/></g>
<g clip-path="url(#sq)">
  <rect x="100" y="100" width="824" height="824" fill="url(#hi)"/>
  <circle cx="{x0}" cy="{y0}" r="70" fill="#fff" fill-opacity="0.12" stroke="#fff" stroke-opacity="0.7" stroke-width="10" stroke-dasharray="22 17"/>
  {''.join(dots)}
  <g filter="url(#tipSh)"><circle cx="{tx}" cy="{ty}" r="{R}" fill="url(#tip)"/></g>
  <g filter="url(#ringSh)">
    <circle cx="{tx}" cy="{ty}" r="{RR}" fill="none" stroke="url(#ring)" stroke-width="34"/>
    <path d="{ticks}" stroke="url(#ring)" stroke-width="34" stroke-linecap="round"/>
  </g>
  <circle cx="{tx}" cy="{ty}" r="38" fill="#ff3b30" stroke="#fff" stroke-width="13"/>
</g>
<rect x="102.5" y="102.5" width="819" height="819" rx="184" fill="none" stroke="url(#edge)" stroke-width="5"/>
'''
open("C3.svg","w").write(f'<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024"><defs>{defs}</defs>{body}</svg>')
