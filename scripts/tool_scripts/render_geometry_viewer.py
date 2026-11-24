"""Self-contained interactive 3D viewer for Sim-BIM source models.

The offline HTML supports orbit and zoom, translucent walls, section cuts,
exploded floors or spaces, vertex measurements, selection and PNG export.
Three.js and OrbitControls are inlined from vendor; model data is embedded
with script-safe JSON encoding, so the viewer opens directly in a browser.

Coincident boundary faces are retained in the model. The viewer hides one of
an overlapping pair at zero separation and displays both in exploded views.
Window offsets keep glazing visible and selectable.

Usage:
    python scripts/tool_scripts/render_geometry_viewer.py source_model.json --out viewer.html
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from scripts.tool_scripts.bim_display_language import (
    room_role, room_type_labels, viewer_markup, viewer_messages, viewer_script,
)

_VENDOR = Path(__file__).resolve().parent / "vendor"


def _js_embed(obj) -> str:
    """JSON for inline embedding in a <script> — escape the chars that could break
    out of the script element / be mis-tokenised (``<`` → ``</script>``, plus & and
    the JS line separators U+2028/U+2029). These become \\u00xx, which a JS object
    literal parses back to the original char."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        .replace(" ", "\\u2028").replace(" ", "\\u2029")
    )


# --------------------------------------------------------------------------- #
# viewer app (classic script; global THREE + THREE.OrbitControls + window.GEO).
# Standalone constant so it can be `node --check`ed.
# --------------------------------------------------------------------------- #
_APP_JS = r"""
(function () {
  const GEO = window.GEO || { zones: [], surfaces: [], windows: [], roles: {} };
  const $ = (id) => document.getElementById(id);
  let locale = 'en';
  const msg = key => GEO.messages[key]?.[locale === 'en' ? 0 : 1] ?? key;
  const SURF = (GEO.surfaces || []).filter(s => (s.verts || []).length >= 3);
  const WINS = (GEO.windows || []).filter(w => (w.verts || []).length >= 3);
  const OPENS = (GEO.openings || []).filter(o => (o.verts || []).length >= 3);
  const WALL_PARTS = GEO.visible_wall_parts || {};
  const ENC_REGIONS = GEO.enclosure_regions || [];
  const ZONES = (GEO.zones || []).slice().sort((a, b) => b.length - a.length);
  const SOURCE = GEO.source_model || null;
  const SOURCE_MAP = SOURCE ? (SOURCE.derived || {}) : {};
  const SOURCE_SPACES = Object.fromEntries(((SOURCE && SOURCE.spaces) || []).map(s=>[s.id,s]));
  const SOURCE_BOUNDARIES = Object.fromEntries(((SOURCE && SOURCE.boundaries) || []).map(b=>[b.id,b]));
  // resolve a window's zone: its parent surface's zone first (parent = "<wall>_<i>"),
  // then a zone-name prefix, then the nearest zone centroid — never returns '?' so a
  // window always pops out + groups/explodes with a real zone.
  const _surfZoneByName = {};
  SURF.forEach(s => { if (s.name) _surfZoneByName[s.name] = s.zone || '?'; });
  const _surfNames = Object.keys(_surfZoneByName).sort((a, b) => b.length - a.length);
  function zoneOfWindow(w){
    const p = w.parent || '';
    for (const n of _surfNames) if (p.startsWith(n)) return _surfZoneByName[n];
    const z = ZONES.find(z => p.startsWith(z)); if (z) return z;
    let cx=0,cy=0,cz=0; const vs=w.verts||[]; vs.forEach(v=>{cx+=v[0];cy+=v[1];cz+=v[2];}); const n=vs.length||1;
    const wc=new THREE.Vector3(cx/n,cy/n,cz/n); let best=ZONES[0]||'?', bd=1e18;
    for (const z in zoneCentroid){ const d=zoneCentroid[z].distanceToSquared(wc); if(d<bd){bd=d;best=z;} }
    return best;
  }

  const FLOOR_COLORS = [0xb0d0e8,0xffe0b2,0xc8e6c9,0xf4c7c7,0xd1c4e9,0xfff59d,0xb2dfdb,0xd7ccc8];
  const TYPE_COLORS = { Wall:0xdfe3e6, Floor:0xc8a165, Ceiling:0x9fa8da, Roof:0xfff3b0 };
  const WINDOW_COLOR = 0x1e5ad2, WHITE = 0xffffff, SEL_COLOR = 0xff9800;
  const ENCLOSURE_COLOR = 0x8b949e, ENCLOSURE_OPACITY = 0.32, LOGICAL_COLOR = 0x596b86;
  // Classification and colors come from the same pinned catalog as generation.
  const ROOM_TYPES = GEO.room_types || {};
  const ROLE_COLORS = Object.fromEntries(Object.entries(ROOM_TYPES).map(([k,v])=>[k,parseInt(v.color.slice(1),16)]));
  const ROLE_DEFAULT = ROLE_COLORS.unknown ?? 0xc9ced4;
  const NAMES = GEO.public_names || {};
  const objectName = n => (NAMES.objects||{})[n] || n;
  function roleLabel(role, bilingual=true){
    const type=ROOM_TYPES[role] || ROOM_TYPES.unknown;
    return locale==='en' ? type.label_en : type.label_zh+(bilingual?' · '+type.label_en:'');
  }
  function spaceName(zone){
    const record=GEO.room_names[zone] || {};
    if(record.name) return record.role!=='unknown' ? roleLabel(record.role,false) : record.name;
    const role=roleOf(zone);
    return role && role!=='unknown' ? roleLabel(role,false) : msg('Room');
  }
  const ROLES = GEO.roles || {};
  const HAS_ROLES = Object.keys(ROLES).length > 0;
  const roleOf = z => (ROLES[z] || '').toString().toLowerCase();
  // zone-mode fill: by room type if roles are available, else white
  const roleColor = z => { if(!HAS_ROLES) return WHITE; const r=roleOf(z); return r ? (ROLE_COLORS[r] ?? ROLE_DEFAULT) : ROLE_DEFAULT; };
  const hex6 = c => '#'+('000000'+(c>>>0).toString(16)).slice(-6);
  const esc = s => String(s).replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  // Geometry is drawn at TRUE positions (so measure is exact + no inset gaps).
  // z-fighting is avoided by: (a) at explode=0, HIDING one face of each coincident
  // reciprocal (split-paired) pair — solid clean shell, no gaps; (b) at explode>0,
  // showing ALL faces (each zone a closed box) separated by the explode offset;
  // (c) windows pushed proud of their wall by WIN_POP (also makes them pickable).
  const WIN_POP = 0.03;  // metres

  // ---- floors: bases from FLOOR-type surfaces, per-zone assignment ----
  const zmin = (s) => Math.min(...s.verts.map(v => v[2]));
  function cluster(zs){ const u=[...new Set(zs.map(z=>Math.round(z*10)/10))].sort((a,b)=>a-b);
    const b=[]; for(const z of u) if(!b.length||Math.abs(z-b[b.length-1])>0.3) b.push(z); return b.length?b:[0]; }
  function nearestBase(z,bases){ let bi=0,bd=1e9; bases.forEach((b,i)=>{const d=Math.abs(z-b); if(d<bd){bd=d;bi=i;}}); return bi; }
  const floorSurf = SURF.filter(s => s.type === 'Floor');
  const NAMED_FLOORS = NAMES.floors || [];
  const BASES = NAMED_FLOORS.length ? NAMED_FLOORS.map(f=>f.z_floor) : cluster((floorSurf.length ? floorSurf : SURF).map(zmin));
  const sourceFloor = i => (SOURCE?.floors||[]).find(f=>f.id===NAMED_FLOORS[i]?.id);
  const floorName = i => sourceFloor(i)?.name || (locale==='en' ? 'Floor '+(i+1) : '第 '+(i+1)+' 层');
  const _zFloorZ = {}, _zMinZ = {};
  SURF.forEach(s => { const z=s.zone||'?', mn=zmin(s); _zMinZ[z]=Math.min(_zMinZ[z]??1e9,mn);
    if(s.type==='Floor') _zFloorZ[z]=Math.min(_zFloorZ[z]??1e9,mn); });
  const zoneFloor = {}, zoneFloors = {};
  Object.keys(_zMinZ).forEach(z => { const sid=(SOURCE_MAP.zones||{})[z]||z;
    const fi=NAMED_FLOORS.findIndex(f=>f.id===SOURCE_SPACES[sid]?.floor_id);
    zoneFloor[z]=fi>=0 ? fi : nearestBase(_zFloorZ[z]??_zMinZ[z], BASES);
    // Filtering follows declared source membership only. A continuous space
    // remains one volume while appearing in every storey that explicitly
    // lists it; cells without section membership use their geometric floor.
    const declared=(GEO.floor_memberships||{})[z]||[];
    zoneFloors[z]=declared.length ? declared : [zoneFloor[z]];
  });
  const zoneForSpace = Object.fromEntries(Object.entries(SOURCE_MAP.zones||{}).map(([z,s])=>[s,z]));
  const verticalCore = z => ['stairwell','shaft'].includes(roleOf(z) || SOURCE_SPACES[(SOURCE_MAP.zones||{})[z]||z]?.role);
  const onFloor = (u,f) => f<0 || (!verticalCore(u.zone) && (zoneFloors[u.zone]||[u.floor]).includes(f));
  function partVisible(u,f,exploded){
    if(!onFloor(u,f)) return false;
    if(exploded || !u.dup) return true;
    if(!u.occludedBy) return false;
    return !u.occludedBy.some(sid=>{const z=zoneForSpace[sid]||sid; return onFloor({zone:z,floor:zoneFloor[z]},f);});
  }

  // ---- zone centroids (explode-by-zone + window pop-out) ----
  const zoneSum = {};
  SURF.forEach(s => { const z=s.zone||'?'; const a=zoneSum[z]||(zoneSum[z]=[0,0,0,0]);
    s.verts.forEach(v=>{a[0]+=v[0];a[1]+=v[1];a[2]+=v[2];a[3]++;}); });
  const zoneCentroid = {};
  Object.keys(zoneSum).forEach(z=>{const a=zoneSum[z]; zoneCentroid[z]=new THREE.Vector3(a[0]/a[3],a[1]/a[3],a[2]/a[3]);});
  // translate a window's verts a few cm AWAY from its zone centroid → proud of the wall
  const popOut = (verts, zone) => { const c=zoneCentroid[zone]; if(!c) return verts;
    let cx=0,cy=0,cz=0; verts.forEach(v=>{cx+=v[0];cy+=v[1];cz+=v[2];}); const n=verts.length||1; cx/=n;cy/=n;cz/=n;
    const dx=cx-c.x, dy=cy-c.y, dz=cz-c.z; const L=Math.hypot(dx,dy,dz)||1; const t=WIN_POP/L;
    return verts.map(v=>[v[0]+dx*t, v[1]+dy*t, v[2]+dz*t]); };

  // ---- bbox ----
  const bb = new THREE.Box3();
  SURF.forEach(s => s.verts.forEach(v => bb.expandByPoint(new THREE.Vector3(v[0],v[1],v[2]))));
  if (bb.isEmpty()) bb.set(new THREE.Vector3(0,0,0), new THREE.Vector3(10,10,3));
  const center = bb.getCenter(new THREE.Vector3());
  const size = bb.getSize(new THREE.Vector3());
  const radius = Math.max(size.x, size.y, size.z) || 10;
  const zoneDir = {};  // full 3D radial from building centre (zone explode = all directions)
  Object.keys(zoneCentroid).forEach(z=>{ const d=zoneCentroid[z].clone().sub(center);
    zoneDir[z] = d.lengthSq()<1e-6 ? new THREE.Vector3(0,0,0) : d.normalize(); });

  // ---- renderer / scene / camera ----
  const renderer = new THREE.WebGLRenderer({ antialias:true, preserveDrawingBuffer:true });
  const viewport=$('app');
  renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));
  renderer.setSize(viewport.clientWidth,viewport.clientHeight,false);
  renderer.localClippingEnabled = true;
  renderer.setClearColor(0xf4f6f8);
  viewport.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(42, viewport.clientWidth/Math.max(1,viewport.clientHeight), 0.05, radius*200);
  camera.up.set(0,0,1);
  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  let cameraMoved=false;
  controls.addEventListener('start',()=>{cameraMoved=true;});
  function defaultView(){
    const direction=new THREE.Vector3(1.6,-1.8,1.3).normalize();
    const right=camera.up.clone().cross(direction).normalize(), up=direction.clone().cross(right);
    const tanY=Math.tan(camera.fov*Math.PI/360), tanX=tanY*camera.aspect;
    let distance=0;
    for(const x of [bb.min.x,bb.max.x]) for(const y of [bb.min.y,bb.max.y]) for(const z of [bb.min.z,bb.max.z]){
      const point=new THREE.Vector3(x,y,z).sub(center);
      const fit=Math.max(Math.abs(point.dot(right))/tanX,Math.abs(point.dot(up))/tanY);
      distance=Math.max(distance,point.dot(direction)+1.25*fit);
    }
    camera.position.copy(center).addScaledVector(direction,Math.max(distance,1));
    controls.target.copy(center);controls.update();cameraMoved=false;
  }
  scene.add(new THREE.AmbientLight(0xffffff, 0.8));
  const dl=new THREE.DirectionalLight(0xffffff,0.6); dl.position.set(1,-1,2); scene.add(dl);
  const dl2=new THREE.DirectionalLight(0xffffff,0.3); dl2.position.set(-1,1,0.5); scene.add(dl2);
  const grid=new THREE.GridHelper(radius*6,76,0xd8dee6,0xe5e9ee); grid.rotateX(Math.PI/2);
  grid.material.transparent=true;grid.material.opacity=.48;grid.material.depthWrite=false;
  grid.position.set(center.x, center.y, bb.min.z); scene.add(grid);

  // ---- small text labels (axes + compass) ----
  function label(txt, color, scl){
    const c=document.createElement('canvas'); c.width=c.height=64; const x=c.getContext('2d');
    x.fillStyle=color; x.font='bold 46px sans-serif'; x.textAlign='center'; x.textBaseline='middle'; x.fillText(txt,32,34);
    const sp=new THREE.Sprite(new THREE.SpriteMaterial({map:new THREE.CanvasTexture(c), depthTest:false, transparent:true}));
    sp.scale.set(scl, scl, 1); return sp;
  }
  // small axis ticks at the origin corner
  const guideSize=Math.max(radius*.35,Math.min(radius,Math.max(size.x,size.y)*1.2));
  const aL=guideSize*.16, aLab=guideSize*.045;
  const axes=new THREE.AxesHelper(aL); axes.position.copy(bb.min); scene.add(axes);
  [['x',0xcc1111,new THREE.Vector3(aL*1.25,0,0)],['y',0x11aa11,new THREE.Vector3(0,aL*1.25,0)],
   ['z',0x1111cc,new THREE.Vector3(0,0,aL*1.25)]].forEach(([t,col,off])=>{
    const s=label(t,'#'+col.toString(16).padStart(6,'0'),aLab); s.position.copy(bb.min).add(off); scene.add(s); });
  // North follows the same +Y reference as the model axes, even after orbiting.
  function orientCompass(){
    camera.updateMatrixWorld();
    const basis=camera.matrixWorld.elements,angle=Math.atan2(basis[1],basis[5]);
    if(Math.hypot(basis[1],basis[5])>1e-5) $('northArrow').style.transform='rotate('+angle+'rad)';
  }
  controls.addEventListener('change',orientCompass);

  // ---- clip planes (section cuts) ----
  const AX = [
    {key:'X', base:new THREE.Vector3(-1,0,0), min:bb.min.x, max:bb.max.x},
    {key:'Y', base:new THREE.Vector3(0,-1,0), min:bb.min.y, max:bb.max.y},
    {key:'Z', base:new THREE.Vector3(0,0,-1), min:bb.min.z, max:bb.max.z},
  ];
  AX.forEach(a=>{ a.enabled=false; a.flip=false; a.pos=a.max; a.plane=new THREE.Plane(a.base.clone(), a.max); });
  function updatePlane(a){ a.plane.normal.copy(a.flip?a.base.clone().negate():a.base.clone()); a.plane.constant=a.flip?-a.pos:a.pos; }
  function activePlanes(){ return AX.filter(a=>a.enabled).map(a=>a.plane); }

  // ---- build meshes (keep ALL faces; reciprocal dup hidden at rest, windows popped out) ----
  const surfMeshes=[], winMeshes=[], openingMeshes=[], enclosureMeshes=[], edgeSegs=[], logicalLines=[], enclosureLines=[], windowEdges=[];
  const root=new THREE.Group(); scene.add(root);
  const surfaceEdges=[];
  function projectionAxis(ring){
    let nx=0,ny=0,nz=0;
    const origin=ring[0]||[0,0,0];
    for(let i=0;i<ring.length;i++){ const a=ring[i].map((v,j)=>v-origin[j]),b=ring[(i+1)%ring.length].map((v,j)=>v-origin[j]);
      nx+=(a[1]-b[1])*(a[2]+b[2]); ny+=(a[2]-b[2])*(a[0]+b[0]); nz+=(a[0]-b[0])*(a[1]+b[1]); }
    const ax=Math.abs(nx), ay=Math.abs(ny), az=Math.abs(nz);
    return ax>=ay && ax>=az ? 0 : ay>=ax && ay>=az ? 1 : 2;
  }
  function projectRing(ring,axis=projectionAxis(ring)){
    return ring.map(v=>axis===0 ? [v[1],v[2]] : axis===1 ? [v[0],v[2]] : [v[0],v[1]]);
  }
  function signedArea2D(pts){ let a=0; for(let i=0;i<pts.length;i++){ const p=pts[i],q=pts[(i+1)%pts.length]; a+=p[0]*q[1]-q[0]*p[1]; } return a/2; }
  function triangulateRings(ring,holes=[]){
    const loops=[ring,...holes].map(r=>r.length>1 && r[0].every((v,i)=>v===r[r.length-1][i]) ? r.slice(0,-1) : r);
    const axis=projectionAxis(loops[0]), projected=loops.map(r=>projectRing(r,axis));
    // A shared local origin avoids cancellation in area validation for valid
    // georeferenced models. The buffer positions retain source coordinates.
    const origin=projected[0][0]?.slice()||[0,0];
    projected.forEach(r=>r.forEach(p=>{p[0]-=origin[0];p[1]-=origin[1];}));
    const vec=r=>r.map(p=>new THREE.Vector2(p[0],p[1]));
    // Earcut handles concave quads and near-collinear returns too. A fan is
    // never a valid fallback for arbitrary source boundaries.
    const indices=THREE.ShapeUtils.triangulateShape(vec(projected[0]),projected.slice(1).map(vec)).flat();
    const points=projected.flat(), expected=Math.abs(signedArea2D(projected[0]))-projected.slice(1).reduce((a,r)=>a+Math.abs(signedArea2D(r)),0);
    let area=0;
    for(let i=0;i<indices.length;i+=3){const [a,b,c]=indices.slice(i,i+3).map(j=>points[j]);
      area+=Math.abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/2;}
    if(Math.abs(area-expected)>Math.max(1e-8,Math.abs(expected)*1e-8)) throw new Error('[[msg:Display triangulation does not cover its boundary]]');
    return {positions:loops.flat().flat(),indices};
  }
  function ringGeom(ring, holes=[]){ const {positions:pos,indices:idx}=triangulateRings(ring,holes);
    const g=new THREE.BufferGeometry(); g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
    g.setIndex(idx); g.computeVertexNormals(); return g; }
  function edgeGeom(ring){ const pos=[]; for(let i=0;i<ring.length;i++){const a=ring[i],b=ring[(i+1)%ring.length];
    pos.push(a[0],a[1],a[2], b[0],b[1],b[2]);} const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3)); return g; }
  function segmentGeom(segments){const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(segments.flat(2),3)); return g;}
  function dashedEdge(ring,color,dash,gap,userData){
    const line=new THREE.LineSegments(edgeGeom(ring),new THREE.LineDashedMaterial({color,transparent:true,opacity:0.95,dashSize:dash,gapSize:gap,depthTest:false}));
    line.computeLineDistances(); line.renderOrder=800; line.userData=userData; root.add(line); return line; }
  const isDup = (s) => s.obc==='Surface' && s.obc_obj && s.name > s.obc_obj;  // one of each reciprocal pair
  SURF.forEach(s=>{
    const zone=s.zone||'?', fi=zoneFloor[zone] ?? nearestBase(zmin(s),BASES);
    // FLAT (unlit) fill so every face of a zone renders the EXACT same colour — no
    // lighting wash that made horizontal (roof/floor) faces read near-white. Edges keep form.
    const m=new THREE.MeshBasicMaterial({side:THREE.DoubleSide, transparent:true, opacity:1});
    const parts = Object.prototype.hasOwnProperty.call(WALL_PARTS,s.name) ? WALL_PARTS[s.name] : [{verts:s.verts,holes:[]}];
    parts.forEach((part,partIndex)=>{
      const dup=part.duplicate_at_rest ?? isDup(s);
      const enclosureCondition=part.enclosure_condition || 'physical';
      // Unknown patches are represented by the enclosure-region helper below.
      // Skipping their projected wall part prevents two coincident viewer faces;
      // the authoritative source boundary and display projection remain untouched.
      if(enclosureCondition==='unknown') return;
      const mesh=new THREE.Mesh(ringGeom(part.verts,part.holes||[]), m.clone());
      mesh.userData={zone, floor:fi, type:s.type||'Wall', name:s.name, kind:'surface', dup,
        occludedBy:part.occluded_by ?? (s.obc_obj ? [_surfZoneByName[s.obc_obj]] : undefined),
        enclosureCondition, publicName:(NAMES.parts||{})[s.name]?.[partIndex] || objectName(s.name),
        area:polyArea(part.verts)-(part.holes||[]).reduce((sum,r)=>sum+polyArea(r),0)};
      surfMeshes.push(mesh); root.add(mesh);
      const segments=part.edge_segments ?? [part.verts,...(part.holes||[])].flatMap(r=>r.map((v,i)=>[v,r[(i+1)%r.length]]));
      const em=new THREE.LineSegments(segmentGeom(segments), new THREE.LineBasicMaterial({color:0x303030}));
      em.userData={...mesh.userData}; edgeSegs.push(em); root.add(em);
      surfaceEdges.push({segments,userData:mesh.userData,name:s.name});
    });
    logicalLines.push(dashedEdge(s.verts,LOGICAL_COLOR,radius*0.018,radius*0.012,
      {zone,floor:fi,kind:'logical',dup:false}));
    m.dispose();
  });
  const enclosureRegionKeys=new Map();
  function enclosureRegionKey(ring){
    return ring.map(v=>v.map(x=>Math.round(x*1e6)/1e6).join(',')).sort().join('|');
  }
  ENC_REGIONS.forEach((r,regionIndex)=>{
    if(!(r.verts||[]).length) return;
    const sourceBoundary=SOURCE_BOUNDARIES[r.boundary_id] || {};
    const zone=r.space_id || sourceBoundary.space_id || '?';
    const fi=zoneFloor[zone] ?? nearestBase(Math.min(...r.verts.map(v=>v[2])),BASES);
    const key=enclosureRegionKey(r.verts), owners=enclosureRegionKeys.get(key)||[], duplicate=owners.length>0;
    enclosureRegionKeys.set(key,[...owners,zone]);
    const userData={zone,floor:fi,kind:'enclosure-region',condition:r.condition,dup:duplicate,
      occludedBy:owners,
      publicName:(NAMES.regions||[])[regionIndex] || objectName(r.boundary_id),
      boundaryId:r.boundary_id,area:polyArea(r.verts),sourceRefs:r.source_refs||[],assumptions:r.assumptions||[],
      evidenceKind:r.evidence_kind,baseColor:ENCLOSURE_COLOR};
    // This translucent face is a viewer-only aid. It makes an open extent easy
    // to see without restoring a physical wall, changing volume, or connectivity.
    const material=new THREE.MeshBasicMaterial({color:ENCLOSURE_COLOR,side:THREE.DoubleSide,
      transparent:true,opacity:ENCLOSURE_OPACITY,depthWrite:false});
    const mesh=new THREE.Mesh(ringGeom(r.verts,r.holes||[]),material); mesh.userData=userData;
    enclosureMeshes.push(mesh); root.add(mesh);
    enclosureLines.push(dashedEdge(r.verts,ENCLOSURE_COLOR,radius*0.012,radius*0.008,{...userData}));
  });
  WINS.forEach(w=>{
    const zone=zoneOfWindow(w), sv=popOut(w.verts, zone);  // proud of wall → clean + pickable
    const m=new THREE.MeshStandardMaterial({color:WINDOW_COLOR, side:THREE.DoubleSide, roughness:0.4});
    const mesh=new THREE.Mesh(ringGeom(sv), m);
    mesh.userData={zone, floor:zoneFloor[zone]??nearestBase(Math.min(...w.verts.map(v=>v[2])),BASES), type:'Window', name:w.name, parent:w.parent, kind:'window', dup:false, area:polyArea(w.verts)};
    winMeshes.push(mesh); root.add(mesh);
    // Comparison contours use the exact source geometry, without the picking offset.
    const edge=new THREE.LineSegments(edgeGeom(w.verts),new THREE.LineBasicMaterial({color:0x087d3b}));
    edge.userData={...mesh.userData}; windowEdges.push(edge); root.add(edge);
  });
  OPENS.forEach(o=>{
    const zone=_surfZoneByName[o.parent], dup=Boolean(o.partner && o.name>o.partner);
    const color=o.kind==='door'?0xa5672a:0x16877b;
    const m=new THREE.MeshBasicMaterial({color,side:THREE.DoubleSide,transparent:true,
      opacity:o.state==='closed'?0.7:0.12,depthWrite:false});
    const mesh=new THREE.Mesh(ringGeom(o.verts),m);
    mesh.userData={zone,floor:zoneFloor[zone]||0,type:o.kind==='door'?'Door':'Passage',name:o.name,
      occludedBy:dup ? [o.other_space_id] : [],
      parent:o.parent,kind:'opening',dup,area:polyArea(o.verts),baseColor:color,sourceId:o.source_opening_id,
      spaceId:o.space_id,otherSpaceId:o.other_space_id,state:o.state};
    openingMeshes.push(mesh);root.add(mesh);
    const em=new THREE.LineSegments(edgeGeom(o.verts),new THREE.LineBasicMaterial({color}));
    em.userData={...mesh.userData};edgeSegs.push(em);root.add(em);
  });
  const allMeshes = () => surfMeshes.concat(winMeshes,openingMeshes,enclosureMeshes);
  const allPickables = () => allMeshes().concat(enclosureLines);

  // The admitted mesh is an offline reference, never a source-BIM entity or pick target.
  const referenceMeshes=[], referenceGroup=new THREE.Group(); scene.add(referenceGroup);
  const reference=GEO.textured_mesh;
  $('meshControls').hidden=!reference;
  let textureFailures=0;
  if(reference){
    const loader=new THREE.TextureLoader();
    const textures=reference.textures.map(uri=>{
      const t=loader.load(uri,undefined,undefined,()=>{textureFailures++; applyFilter();});
      t.wrapS=t.wrapT=THREE.RepeatWrapping;
      // Payload RGB already includes the linear base-colour factor and is sRGB encoded.
      // The existing unlit viewer uses LinearEncoding for both maps and output.
      t.encoding=THREE.LinearEncoding; return t;
    });
    reference.parts.forEach(part=>{
      const geometry=new THREE.BufferGeometry();
      geometry.setAttribute('position',new THREE.Float32BufferAttribute(part.positions,3));
      geometry.setAttribute('uv',new THREE.Float32BufferAttribute(part.uv,2));
      geometry.setIndex(part.indices);
      const material=new THREE.MeshBasicMaterial({map:textures[part.texture],side:THREE.DoubleSide,
        polygonOffset:true,polygonOffsetFactor:1,polygonOffsetUnits:1});
      const mesh=new THREE.Mesh(geometry,material);
      mesh.userData={alphaMode:part.alpha_mode,alphaFactor:part.alpha_factor};
      referenceMeshes.push(mesh); referenceGroup.add(mesh);
    });
  }
  const meshActive = () => Boolean(reference && $('showMesh').checked && parseFloat($('explode').value)===0);
  function comparisonPlanes(){
    const p=activePlanes().slice(), f=parseInt($('floorSel').value,10), floor=sourceFloor(f);
    if(floor && Number.isFinite(floor.height) && parseFloat($('explode').value)===0){
      const pad=Math.max(1e-6,Math.abs(floor.z_floor+floor.height)*2e-7);
      p.push(new THREE.Plane(new THREE.Vector3(0,0,1),-floor.z_floor+pad),
        new THREE.Plane(new THREE.Vector3(0,0,-1),floor.z_floor+floor.height+pad));
    }
    return p;
  }

  function applyClipping(){ const p=comparisonPlanes();
    allMeshes().forEach(m=>{m.material.clippingPlanes=p; m.material.needsUpdate=true;});
    referenceMeshes.forEach(m=>{m.material.clippingPlanes=p; m.material.needsUpdate=true;});
    edgeSegs.concat(logicalLines,enclosureLines,windowEdges).forEach(e=>{e.material.clippingPlanes=p; e.material.needsUpdate=true;}); }

  // ---- geometric measures (on TRUE geometry) ----
  function polyArea(r){ let nx=0,ny=0,nz=0; const n=r.length;  // Newell → planar polygon area
    for(let i=0;i<n;i++){ const a=r[i],b=r[(i+1)%n]; nx+=(a[1]-b[1])*(a[2]+b[2]); ny+=(a[2]-b[2])*(a[0]+b[0]); nz+=(a[0]-b[0])*(a[1]+b[1]); }
    return Math.hypot(nx,ny,nz)/2; }
  // ---- select-by modes + uniform-colour selection ----
  let selected=new Set(); const selGroup=new THREE.Group(); scene.add(selGroup);
  function refreshColors(){
    const mode=$('colorBy').value;
    surfMeshes.forEach(m=>{ let c; if(mode==='floor') c=FLOOR_COLORS[m.userData.floor%FLOOR_COLORS.length];
      else if(mode==='zone') c=roleColor(m.userData.zone);   // colour by room type
      else if(mode==='edge') c=WHITE; else c=TYPE_COLORS[m.userData.type] ?? 0xcccccc;
      m.userData.baseColor=c; });
    winMeshes.forEach(m=>m.userData.baseColor=WINDOW_COLOR);
    enclosureMeshes.forEach(m=>m.userData.baseColor=ENCLOSURE_COLOR);
    allPickables().forEach(m=>m.material.color.setHex(selected.has(m) ? SEL_COLOR : m.userData.baseColor));
    updateLegend(mode);
  }
  function clearSelGroup(){ while(selGroup.children.length) selGroup.remove(selGroup.children[0]); }
  let inspected=null;
  function setSelection(arr){ clearSelGroup(); selected=new Set(arr);
    allPickables().forEach(m=>m.material.color.setHex(selected.has(m)?SEL_COLOR:(m.userData.baseColor??0xcccccc)));
  }
  function clearSelection(){ setSelection([]); inspected=null; renderInspection(); }

  // ---- measure (button; CONTINUOUS; CAD-style screen-space vertex snap on true geometry) ----
  let measuring=false, measurePts=[]; const mGroup=new THREE.Group(); scene.add(mGroup);
  // overlay helpers: always-on-top (depthTest off + depthWrite off + high renderOrder) so a
  // highlight is NEVER hidden behind the (transparent) faces — no need to rotate to see it.
  function topMat(color){ return new THREE.MeshBasicMaterial({color, depthTest:false, depthWrite:false, transparent:true}); }
  function topBall(p, color, rmul){ const s=new THREE.Mesh(new THREE.SphereGeometry(radius*(rmul||0.010),16,16), topMat(color));
    s.position.copy(p); s.renderOrder=999; return s; }
  // a THICK highlight line as a thin cylinder — WebGL ignores LineMaterial.linewidth, so a
  // real tube is the only way to get a visibly bold line that reads at any camera angle.
  function fatLine(a, b, color, rmul){ const dir=new THREE.Vector3().subVectors(b,a); const len=dir.length()||1e-6; const r=radius*(rmul||0.005);
    const m=new THREE.Mesh(new THREE.CylinderGeometry(r, r, len, 8), topMat(color));
    m.position.copy(a).addScaledVector(dir, 0.5);
    m.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0), dir.clone().normalize()); m.renderOrder=999; return m; }
  const snap=topBall(new THREE.Vector3(), 0x00e5ff, 0.011); snap.visible=false; scene.add(snap);
  const CAND=[];  // {v: true world Vector3, zone}
  SURF.forEach(s=>s.verts.forEach(v=>CAND.push({v:new THREE.Vector3(v[0],v[1],v[2]), zone:s.zone||'?'})));
  WINS.forEach(w=>{const z=zoneOfWindow(w); w.verts.forEach(v=>CAND.push({v:new THREE.Vector3(v[0],v[1],v[2]), zone:z}));});
  OPENS.forEach(o=>o.verts.forEach(v=>CAND.push({v:new THREE.Vector3(...v),zone:_surfZoneByName[o.parent]})));
  function clearPair(){ while(mGroup.children.length) mGroup.remove(mGroup.children[0]); measurePts=[]; }
  function clearMeasure(){ measuring=false; snap.visible=false; clearPair();
    $('meas').style.display='none'; renderer.domElement.style.cursor='default'; }
  function startMeasure(){ clearMeasure(); measuring=true; renderer.domElement.style.cursor='crosshair';
    $('meas').style.display='block'; renderMeasurement(); }
  function renderMeasurement(){
    if(!measuring) return;
    const distance=measurePts.length===2 ? measurePts[0].distanceTo(measurePts[1]) : null;
    $('meas').textContent=distance!==null ? '[[msg:distance: ]]'+distance.toFixed(3)+'[[msg: m  (click=next, Esc=exit)]]' :
      (measurePts.length ? '[[msg:measure: snap vertex 2  (Esc to exit)]]' : '[[msg:measure: snap vertex 1  (Esc to exit)]]');
  }
  function marker(p){ mGroup.add(topBall(p, 0xd81b60, 0.011)); }
  // Picking follows wall opacity: when walls are OPAQUE you can only snap/select what is visibly
  // in front (a candidate hidden behind a face is skipped); turn DOWN wall opacity to reach the
  // geometry behind. Keeps the precise screen-nearest aim within the tolerance.
  const _ray=new THREE.Raycaster();
  function occluded(w){ const from=camera.position; const dir=w.clone().sub(from); const dist=dir.length()||1e-6;
    _ray.set(from, dir.divideScalar(dist)); _ray.near=0; _ray.far=dist-radius*0.01;   // only faces strictly IN FRONT of w
    return _ray.intersectObjects(surfMeshes.filter(m=>m.visible), false)
      .some(h=>comparisonPlanes().every(p=>p.distanceToPoint(h.point)>=-0.0001)); }
  function seeThrough(){ return parseFloat($('opacity').value) < 1; }   // transparent → allowed to reach behind
  function snapPick(cx, cy){ const r=renderer.domElement.getBoundingClientRect(); const thru=seeThrough();
    let best=null, bd=24*24;
    for(const c of CAND){ const w=c.v.clone().add(explodeOffset(c.zone)); const p=w.clone().project(camera);
      if(meshActive() && $('meshMode').value==='mesh') continue;
      if(!onFloor({zone:c.zone,floor:zoneFloor[c.zone]},parseInt($('floorSel').value,10))) continue;
      if(!comparisonPlanes().every(plane=>plane.distanceToPoint(w)>=-0.0001)) continue;
      if(p.z<-1||p.z>1) continue; const sx=(p.x*0.5+0.5)*r.width, sy=(-p.y*0.5+0.5)*r.height;
      const d=(sx-cx)**2+(sy-cy)**2; if(d<bd && (thru || !occluded(w))){ bd=d; best=w; } }
    return best; }

  // ---- edge select (screen-space nearest segment → length) ----
  const EDGES=[];  // {a,b: true world Vector3, zone, len, floor, dup, kind}
  function pushEdges(verts, zone, floor, dup, kind, name){ for(let i=0;i<verts.length;i++){ const a=verts[i], b=verts[(i+1)%verts.length];
    EDGES.push({a:new THREE.Vector3(a[0],a[1],a[2]), b:new THREE.Vector3(b[0],b[1],b[2]), zone, floor, dup, kind,
      publicName:(NAMES.edges||{})[name]?.[i] || objectName(name)+"_Edge"+(i+1), parentName:objectName(name), sourceName:name,
      len:Math.hypot(a[0]-b[0],a[1]-b[1],a[2]-b[2])}); } }
  surfaceEdges.forEach(({segments,userData:u,name})=>segments.forEach(([a,b],i)=>{
    EDGES.push({...u,a:new THREE.Vector3(...a),b:new THREE.Vector3(...b),
      publicName:u.publicName+'_Edge'+(i+1),parentName:objectName(name),sourceName:name,
      len:Math.hypot(a[0]-b[0],a[1]-b[1],a[2]-b[2])});
  }));
  WINS.forEach(w=>{ const z=zoneOfWindow(w); pushEdges(w.verts, z, zoneFloor[z]??nearestBase(Math.min(...w.verts.map(v=>v[2])),BASES), false, 'window', w.name); });
  OPENS.forEach(o=>{const z=_surfZoneByName[o.parent], start=EDGES.length;
    pushEdges(o.verts,z,zoneFloor[z]||0,Boolean(o.partner&&o.name>o.partner),'opening',o.name);
    EDGES.slice(start).forEach(e=>e.occludedBy=[o.other_space_id]);});
  // only pick an edge whose source face is currently shown (same predicate as applyFilter)
  function edgeVisible(e){ const f=parseInt($('floorSel').value,10); const exploded=parseFloat($('explode').value)>0;
    if(meshActive() && ($('meshMode').value==='mesh'||!$('showEdges').checked)) return false;
    if(!partVisible(e,f,exploded)) return false;
    return (e.kind==='window') ? $('showWin').checked : (e.kind==='opening') ? $('showOpen').checked : $('showWalls').checked; }
  function segDist(px,py,ax,ay,bx,by){ const dx=bx-ax,dy=by-ay, L2=dx*dx+dy*dy||1;
    let t=((px-ax)*dx+(py-ay)*dy)/L2; t=Math.max(0,Math.min(1,t));
    return (px-(ax+t*dx))**2 + (py-(ay+t*dy))**2; }
  function edgePick(ev){ const r=renderer.domElement.getBoundingClientRect();
    const cx=ev.clientX-r.left, cy=ev.clientY-r.top; const thru=seeThrough(); let best=null, bd=18*18;
    for(const e of EDGES){ if(!edgeVisible(e)) continue; const o=explodeOffset(e.zone);
      const A=e.a.clone().add(o), B=e.b.clone().add(o);
      if(!comparisonPlanes().every(p=>p.distanceToPoint(A)>=-0.0001 && p.distanceToPoint(B)>=-0.0001)) continue;
      const pa=A.clone().project(camera), pb=B.clone().project(camera);
      if(pa.z<-1||pa.z>1||pb.z<-1||pb.z>1) continue;  // skip if EITHER endpoint is behind/clipped
      const ax=(pa.x*.5+.5)*r.width, ay=(-pa.y*.5+.5)*r.height, bx=(pb.x*.5+.5)*r.width, by=(-pb.y*.5+.5)*r.height;
      const d=segDist(cx,cy,ax,ay,bx,by);   // opaque: skip an edge hidden behind a face (use its midpoint)
      if(d<bd && (thru || !occluded(A.clone().add(B).multiplyScalar(0.5)))){ bd=d; best={e,A,B}; } }
    clearSelection(); if(!best) return;
    selGroup.add(fatLine(best.A, best.B, SEL_COLOR, 0.006));   // whole edge as one thick, always-on-top line
    showInspection('edge',best.e); }

  // ---- selection picking (face raycast, for click-select only) ----
  const raycaster=new THREE.Raycaster();
  raycaster.params.Line.threshold=radius*0.012;
  function pick(ev){ const r=renderer.domElement.getBoundingClientRect();
    const mouse=new THREE.Vector2(((ev.clientX-r.left)/r.width)*2-1, -((ev.clientY-r.top)/r.height)*2+1);
    raycaster.setFromCamera(mouse,camera);
    const hits=raycaster.intersectObjects(allPickables().filter(m=>m.visible),false)
      .filter(h=>comparisonPlanes().every(p=>p.distanceToPoint(h.point)>=-0.0001)); return hits.length?hits[0]:null; }
  const surfaceByName=new Map(SURF.map(s=>[s.name,s]));
  const openingByName=new Map(OPENS.map(o=>[o.name,o]));
  const sourceOpenings=new Map((SOURCE?.openings||[]).map(o=>[o.id,o]));
  function component(u){
    const key=u.name || u.sourceName;
    const surface=surfaceByName.get(key);
    if(u.kind==='window') return 'Window';
    if(u.kind==='opening') return openingByName.get(key)?.kind==='door' ? 'Door' : 'Passage';
    if(u.kind==='enclosure-region') return u.condition==='open' ? 'Open boundary' : 'Unknown enclosure';
    const type=u.type || surface?.type;
    if(type==='Wall') return surface?.obc==='Outdoors' ? 'Exterior wall' : (surface?.obc==='Surface' ? 'Interior wall' : 'Wall');
    return ({Floor:'Floor slab',Roof:'Roof',Ceiling:'Ceiling'})[type] || 'Boundary';
  }
  function declaredStoreys(zone){
    const floors=zoneFloors[zone];
    if(floors.length<2 || !verticalCore(zone)) return floors;
    const space=SOURCE_SPACES[(SOURCE_MAP.zones||{})[zone]||zone];
    if(!space) return floors;
    // A vertical core can own a full-height source group in addition to its declared storeys.
    return floors.filter(i=>{const f=sourceFloor(i);
      return f?.id!==space.floor_id || ZONES.some(z=>zoneFloor[z]===i && !verticalCore(z)) ||
        !floors.some(j=>j!==i && sourceFloor(j)?.height < f.height-0.001);
    });
  }
  const storeysByZone=Object.fromEntries(Object.keys(zoneFloors).map(z=>[z,declaredStoreys(z)]));
  const actualStoreys=(zone,fallback)=>storeysByZone[zone]||[fallback];
  function selectionFloors(mode,item,u){
    const floors=actualStoreys(u.zone,u.floor);
    if(mode==='zone' || floors.length<2) return floors;
    let low,high;
    if(item.a && item.b){low=Math.min(item.a.z,item.b.z);high=Math.max(item.a.z,item.b.z);}
    else if(item.geometry){if(!item.geometry.boundingBox) item.geometry.computeBoundingBox();
      low=item.geometry.boundingBox.min.z;high=item.geometry.boundingBox.max.z;}
    if(!Number.isFinite(low)) return floors;
    const overlap=floors.filter(i=>{const f=sourceFloor(i);return f && low<f.z_floor+f.height-0.001 && high>f.z_floor+0.001;});
    if(overlap.length) return overlap;
    const touching=floors.filter(i=>{const f=sourceFloor(i);return f && low<=f.z_floor+f.height+0.001 && high>=f.z_floor-0.001;});
    return touching.length ? touching : floors;
  }
  function authoredComponent(u){
    const key=u.name || u.sourceName;
    const source=(u.kind==='window' || u.kind==='opening') ?
      sourceOpenings.get(u.sourceId || SOURCE_MAP.windows?.[key] || SOURCE_MAP.openings?.[key] || key) :
      SOURCE_BOUNDARIES[u.boundaryId || SOURCE_MAP.surfaces?.[key] || key];
    const name=source?.display_name || source?.name || source?.label;
    return name && !/^[ZS]\d+(?:$|[_:/.-])|[_:]|\/(?:wall|floor|roof|boundary)\//i.test(name) ? name : '';
  }
  function showInspection(mode,item,floor){inspected={mode,item,floor};renderInspection();}
  function renderInspection(){
    const el=$('sel'); el.style.display=inspected ? 'block' : 'none';
    if(!inspected){el.replaceChildren();return;}
    const {mode,item}=inspected, u=item.userData || item;
    const floors=selectionFloors(mode,item,u);
    let storey=floors.map(floorName).join(' / '),name,use;
    if(mode==='zone'){name=spaceName(u.zone);use=roleLabel(roleOf(u.zone));}
    else if(mode==='floor'){
      const i=inspected.floor;
      name=storey=floorName(i);
      const roles=new Set(ZONES.filter(z=>(storeysByZone[z]||[]).includes(i)).map(roleOf));
      use=[...roles].map(r=>roleLabel(r)).join(' · ')||msg('Storey');
    }else{
      const type=msg(component(u));
      name=authoredComponent(u) || spaceName(u.zone)+' · '+type;
      use=type;
      if(mode==='edge'){name+=' · '+msg('Edge');use=type+' · '+msg('Edge');}
    }
    el.innerHTML=[[msg('Name'),name],[msg('Storey'),storey],[msg('Function'),use]]
      .map(([key,value])=>row(esc(key),esc(value))).join('');
  }
  function handleClick(ev){
    if(measuring){ const r=renderer.domElement.getBoundingClientRect();
      const p=snapPick(ev.clientX-r.left, ev.clientY-r.top); if(!p) return;
      if(measurePts.length>=2) clearPair();  // continuous: a new click starts a fresh pair (only latest shown)
      marker(p); measurePts.push(p);
      if(measurePts.length===2) mGroup.add(fatLine(measurePts[0],measurePts[1],0xd81b60,0.004));
      renderMeasurement();
      return; }
    const mode=$('colorBy').value;
    if(mode==='edge'){ edgePick(ev); return; }
    const hit=pick(ev); if(!hit){ clearSelection(); return; }
    const o=hit.object;
    let sel, floor;
    if(mode==='floor'){
      const floors=actualStoreys(o.userData.zone,o.userData.floor), active=Number($('floorSel').value);
      const z=hit.point.z-o.position.z;
      floor=floors.includes(active) ? active : (floors.find(i=>{const f=sourceFloor(i);
        return f && z>=f.z_floor-0.001 && z<f.z_floor+f.height-0.001;
      }) ?? floors[floors.length-1]);
      sel=allPickables().filter(m=>actualStoreys(m.userData.zone,m.userData.floor).includes(floor));
    }else if(mode==='zone') sel=allPickables().filter(m=>m.userData.zone===o.userData.zone); else sel=[o];
    setSelection(sel); showInspection(mode,o,floor);
  }
  // click vs orbit-drag: only a near-stationary press is a click
  let downXY=null;
  renderer.domElement.addEventListener('pointerdown', e=>{ if(e.button===0) downXY=[e.clientX,e.clientY]; });
  renderer.domElement.addEventListener('pointerup', e=>{ if(e.button!==0||!downXY) return;
    const moved=Math.hypot(e.clientX-downXY[0], e.clientY-downXY[1]); downXY=null; if(moved<=5) handleClick(e); });
  renderer.domElement.addEventListener('pointermove', e=>{ if(!measuring) return;
    const r=renderer.domElement.getBoundingClientRect(); const p=snapPick(e.clientX-r.left, e.clientY-r.top);
    if(p){ snap.position.copy(p); snap.visible=true; } else snap.visible=false; });

  // ---- display toggles + explode ----
  // At explode=0 the duplicate of each coincident reciprocal pair is HIDDEN (solid
  // clean shell, no gaps, no z-fight); at explode>0 ALL faces show (each zone a
  // closed box) separated by the offset.
  function applyFilter(){ const f=parseInt($('floorSel').value,10);
    const exploded=parseFloat($('explode').value)>0;
    const comparison=meshActive(), mode=$('meshMode').value;
    const fills=!comparison || mode==='blend', contours=!comparison || mode!=='mesh';
    const sw=$('showWalls').checked, swin=$('showWin').checked, se=$('showEdges').checked;
    const okF=(u)=>partVisible(u,f,exploded);
    surfMeshes.forEach(m=>m.visible = fills && sw && okF(m.userData));
    winMeshes.forEach(m=>m.visible = fills && swin && okF(m.userData));
    openingMeshes.forEach(m=>m.visible=fills && $('showOpen').checked && okF(m.userData));
    enclosureMeshes.forEach(m=>m.visible=fills && $('showEnclosure').checked && okF(m.userData));
    edgeSegs.forEach(e=>{e.visible = contours && se && okF(e.userData) && (e.userData.kind!=='opening'||$('showOpen').checked);
      e.material.color.setHex(e.userData.kind==='opening' ? (comparison?0xec820e:e.userData.baseColor) : (comparison?0xc51b80:0x303030));});
    windowEdges.forEach(e=>e.visible=comparison && contours && se && swin && okF(e.userData));
    logicalLines.forEach(e=>e.visible=contours && $('showLogical').checked && okF(e.userData));
    enclosureLines.forEach(e=>e.visible=contours && $('showEnclosure').checked && okF(e.userData));
    referenceGroup.visible=comparison;
    referenceMeshes.forEach(m=>{const v=parseFloat($('meshOpacity').value)*m.userData.alphaFactor;
      m.material.opacity=v; m.material.transparent=v<1||m.userData.alphaMode==='BLEND';
      m.material.depthWrite=!m.material.transparent;});
    $('meshStatus').textContent=textureFailures?'[[msg:贴图加载失败，请重新打开完整 HTML。]]':
      ($('showMesh').checked&&exploded?'[[msg:展开时暂停贴图叠加]]':
      (comparison?'[[msg:粉色：BIM边界 · 绿色：窗 · 橙色：门；遮挡处可剖切查看。]]':''));
    applyClipping(); }
  function explodeOffset(zone){ const amt=parseFloat($('explode').value); if(amt<=0) return new THREE.Vector3();
    if($('explodeMode').value==='floor') return new THREE.Vector3(0,0, (zoneFloor[zone]||0)*amt*radius*1.0);
    return (zoneDir[zone]||new THREE.Vector3()).clone().multiplyScalar(amt*radius*1.2);  // zone: full 3D radial
  }
  function applyExplode(){ surfMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    winMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    openingMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    enclosureMeshes.forEach(m=>m.position.copy(explodeOffset(m.userData.zone)));
    edgeSegs.concat(logicalLines,enclosureLines).forEach(e=>e.position.copy(explodeOffset(e.userData.zone)));
    applyFilter(); }  // re-evaluate dup visibility when crossing explode 0 ↔ >0

  // ---- right-side info (structured) ----
  function row(k,v){ return '<div class="kv"><span>'+k+'</span><b>'+v+'</b></div>'; }
  function updateStats(){
    const count=n=>n.toLocaleString(GEO.locale);
    $('counts').innerHTML=row('[[msg:zones]]',count((GEO.zones||[]).length))+row('[[msg:surfaces]]',count(SURF.length))+
      row('[[msg:windows]]',count(WINS.length))+row(SOURCE?'[[msg:Elevation groups]]':'[[msg:floors]]',count(BASES.length));
    $('dimensions').innerHTML=row('[[msg:Width]]',size.x.toFixed(2)+' m')+row('[[msg:Depth]]',size.y.toFixed(2)+' m')+
      row('[[msg:Height]]',size.z.toFixed(2)+' m');
    $('elementStats').hidden=!SOURCE;
    if(SOURCE) $('elementCounts').innerHTML=
      row('[[msg:Spaces]]',count(SOURCE.spaces.length))+row('[[msg:Boundaries]]',count(SOURCE.boundaries.length))+
      row('[[msg:Openings]]',count(SOURCE.openings.length))+row('[[msg:Connections]]',count((SOURCE.connections||[]).length))+
      (SOURCE.schema_version==='source_bim_v3' ? row('[[msg:半开敞 / 开敞空间]]',count(SOURCE.spaces.filter(s=>['semi_open','open'].includes(s.enclosure)).length))+
        row('[[msg:开敞 / 未知边界]]',count(SOURCE.boundaries.filter(b=>['open','unknown','mixed'].includes(b.enclosure)).length)) : '');
  }
  updateStats();

  // ---- room-type legend (shown in zone mode: colour swatch → room type) ----
  function updateLegend(mode){
    const el=$('legend'); if(!el) return;
    $('legendGroup').hidden=mode!=='zone' || !HAS_ROLES;
    if($('legendGroup').hidden){el.innerHTML='';return;}
    const present=[], seen=new Set();
    (GEO.zones||[]).forEach(z=>{ const r=roleOf(z)||'untyped'; if(!seen.has(r)){ seen.add(r); present.push(r);} });
    present.sort();
    let h='';
    present.forEach(r=>{ const c=(r==='untyped')?ROLE_DEFAULT:(ROLE_COLORS[r]??ROLE_DEFAULT);
      h+='<div class="lg"><span class="sw" style="background:'+hex6(c)+'"></span>'+esc(roleLabel(r))+'</div>'; });
    el.innerHTML=h;
  }

  // ---- section controls ----
  const secDiv=$('sections');
  AX.forEach(a=>{ const wrap=document.createElement('div'); wrap.className='sec'; const step=((a.max-a.min)/200)||0.01;
    wrap.innerHTML='<label class="axis-label"><input type="checkbox" id="en'+a.key+'"><b>'+a.key+'</b></label>'+
      '<input type="range" aria-label="'+a.key+'" id="pos'+a.key+'" min="'+a.min+'" max="'+a.max+'" step="'+step+'" value="'+a.max+'">'+
      '<label class="chk flip"><input type="checkbox" id="flip'+a.key+'"> <span data-i18n="flip side">[[msg:flip side]]</span></label>';
    secDiv.appendChild(wrap);
    const refresh=()=>{a.enabled=$('en'+a.key).checked; a.flip=$('flip'+a.key).checked; a.pos=parseFloat($('pos'+a.key).value); updatePlane(a); applyClipping();};
    $('en'+a.key).onchange=refresh; $('pos'+a.key).oninput=refresh; $('flip'+a.key).onchange=refresh; });

  // ---- floor select + wiring ----
  const fs=$('floorSel'); BASES.forEach((b,i)=>{
    if(!SURF.some(s=>onFloor({zone:s.zone,floor:zoneFloor[s.zone]},i))) return;
    const o=document.createElement('option'); o.value=i; o.textContent=floorName(i)+' (z='+b.toFixed(2)+')'; fs.appendChild(o);});
  $('colorBy').onchange=()=>{ refreshColors(); clearSelection(); };
  fs.onchange=()=>{clearSelection();clearMeasure();applyFilter();};
  $('showWalls').onchange=applyFilter; $('showWin').onchange=applyFilter; $('showEdges').onchange=applyFilter;
  $('showOpen').onchange=applyFilter; $('showLogical').onchange=applyFilter; $('showEnclosure').onchange=applyFilter;
  $('showMesh').onchange=applyFilter; $('meshMode').onchange=applyFilter; $('meshOpacity').oninput=applyFilter;
  $('opacity').oninput=e=>{const v=parseFloat(e.target.value); surfMeshes.forEach(m=>{
    m.material.opacity=v; m.material.transparent=v<1;});};
  $('explode').oninput=applyExplode; $('explodeMode').onchange=applyExplode;
  for(const id of ['opacity','explode','meshOpacity']){
    const input=$(id),output=$(id+'Value');
    const showValue=()=>{
      output.value=Math.round(Number(input.value)*100)+'%';
      const fill=(Number(input.value)-Number(input.min))/(Number(input.max)-Number(input.min));
      input.style.setProperty('--progress',fill*100+'%');
    };
    input.addEventListener('input',showValue);showValue();
  }
  $('measure').onclick=()=>{ measuring ? clearMeasure() : startMeasure(); };  // toggle
  $('clearMeasure').onclick=clearMeasure;
  window.addEventListener('keydown', e=>{ if(e.key==='Escape' && measuring) clearMeasure(); });
  $('reset').onclick=defaultView;
  $('savePng').onclick=()=>{ renderer.render(scene,camera); const a=document.createElement('a');
    a.download='geometry_view.png'; a.href=renderer.domElement.toDataURL('image/png'); a.click(); };

  function applyLocale(next){
    locale=next==='zh-CN' ? 'zh-CN' : 'en';
    GEO.locale=locale;
    document.documentElement.lang=locale;
    $('locale').value=locale;
    document.querySelectorAll('[data-i18n]').forEach(el=>{el.textContent=msg(el.dataset.i18n);});
    for(const attr of ['title','aria-label','placeholder']){
      document.querySelectorAll('[data-i18n-'+attr+']').forEach(el=>el.setAttribute(attr,msg(el.getAttribute('data-i18n-'+attr))));
    }
    Array.from(fs.options).filter(o=>Number(o.value)>=0).forEach(o=>{o.textContent=floorName(Number(o.value));});
    updateStats();updateLegend($('colorBy').value);renderInspection();renderMeasurement();applyFilter();
  }
  $('locale').onchange=e=>applyLocale(e.target.value);
  applyLocale('en');
  refreshColors(); applyExplode(); defaultView();
  new ResizeObserver(()=>{
    const w=viewport.clientWidth,h=viewport.clientHeight;
    if(!w || !h) return;
    camera.aspect=w/h;camera.updateProjectionMatrix();renderer.setSize(w,h,false);
    if(!cameraMoved) defaultView();
  }).observe(viewport);
  (function loop(){ requestAnimationFrame(loop); controls.update(); renderer.render(scene,camera); })();
})();
"""

_PANEL_HTML = r"""
<header class="masthead">
  <h1><svg class="brand-mark" aria-hidden="true" viewBox="0 0 32 32"><path fill="currentColor" d="M0 19 9 15v15H0zm10-3 6-4v18h-6zM16 2h15v15H16z"/><path fill="none" stroke="currentColor" stroke-width="1.5" d="M23.5 17H31v13h-7.5z"/></svg>Sim-BIM</h1>
  <div class="head-actions">
    <select id="locale" aria-label="[[msg:Language]]"><option value="en" lang="en">English</option><option value="zh-CN" lang="zh-CN">中文</option></select>
    <button type="button" id="savePng"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/></svg><span>[[msg:Export PNG]]</span></button>
  </div>
</header>
<main class="viewer">
  <section class="stage">
    <div id="app"></div>
    <div class="compass" aria-hidden="true"><span>N</span><svg viewBox="0 0 24 24"><g id="northArrow"><path fill="#34445a" d="m12 2 4 12-4-2-4 2z"/><path fill="#b5bfcb" d="m12 22-4-12 4 2 4-2z"/></g></svg></div>
    <div class="view-filters">
      <label class="filter" for="colorBy"><span>[[msg:select by]]</span><select id="colorBy"><option value="zone">[[msg:zone]]</option><option value="floor">[[msg:floor]]</option><option value="surface">[[msg:surface]]</option><option value="edge">[[msg:edge]]</option></select></label>
      <label class="filter" for="floorSel"><span>[[msg:楼层]]</span><select id="floorSel"><option value="-1">[[msg:all floors]]</option></select></label>
    </div>
    <div class="stage-tools">
      <div id="meas" role="status"></div>
      <div class="tool-row">
        <button type="button" id="measure"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m3 16 13-13 5 5L8 21zM7 12l2 2m1-5 2 2m1-5 2 2"/></svg><span>[[msg:Measure]]</span></button>
        <button type="button" id="clearMeasure">[[msg:clear measure]]</button>
        <button type="button" id="reset"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 10a8 8 0 1 1 1 7M4 4v6h6"/></svg><span>[[msg:reset view]]</span></button>
      </div>
    </div>
    <div id="sel" aria-live="polite"></div>
  </section>
  <aside id="panel">
    <div id="rinfo">
      <section id="hud">
        <h2>[[msg:Statistics]]</h2>
        <div id="counts"></div>
        <div id="dimensions" aria-label="[[msg:Dimensions]]"></div>
        <details id="elementStats"><summary>[[msg:Building elements]]</summary><div id="elementCounts"></div></details>
      </section>
    </div>
    <section class="settings">
      <h2>[[msg:View settings]]</h2>
      <details open><summary>[[msg:display]]</summary>
        <div class="parameter">
          <label for="opacity">[[msg:wall opacity]]</label><output id="opacityValue" for="opacity">100%</output>
          <input type="range" id="opacity" min="0.05" max="1" step="0.05" value="1">
        </div>
        <div class="parameter">
          <label for="explode">[[msg:exploded view]]</label><output id="explodeValue" for="explode">0%</output>
          <input type="range" id="explode" min="0" max="1" step="0.02" value="0">
          <select id="explodeMode" aria-label="[[msg:exploded view]]"><option value="floor">[[msg:by floor]]</option><option value="zone">[[msg:by zone]]</option></select>
        </div>
      </details>
      <details><summary>[[msg:Layers]]</summary><div class="checks">
        <label class="chk"><input type="checkbox" id="showWalls" checked> [[msg:wall / floor / roof faces]]</label>
        <label class="chk"><input type="checkbox" id="showWin" checked> [[msg:windows]]</label>
        <label class="chk"><input type="checkbox" id="showOpen" checked> [[msg:门 / 空开口]]</label>
        <label class="chk"><input type="checkbox" id="showEdges" checked> [[msg:edges]]</label>
        <label class="chk"><input type="checkbox" id="showLogical"> [[msg:逻辑空间边界（细虚线）]]</label>
        <label class="chk"><input type="checkbox" id="showEnclosure" checked> [[msg:开敞 / 未知辅助面]]</label>
      </div></details>
      <details><summary>[[msg:section cuts]]</summary><div id="sections"></div></details>
      <details id="meshControls" hidden><summary>[[msg:Comparison]]</summary>
        <div class="checks"><label class="chk"><input type="checkbox" id="showMesh"> [[msg:叠加原始 textured mesh]]</label></div>
        <select id="meshMode" aria-label="[[msg:Comparison]]"><option value="outline">[[msg:贴图 + BIM轮廓]]</option><option value="blend">[[msg:贴图 + BIM实体]]</option><option value="mesh">[[msg:仅贴图]]</option></select>
        <div class="parameter">
          <label for="meshOpacity">[[msg:贴图透明度]]</label><output id="meshOpacityValue" for="meshOpacity">100%</output>
          <input type="range" id="meshOpacity" min="0.1" max="1" step="0.05" value="1">
        </div>
        <p id="meshStatus" role="status"></p>
      </details>
      <details id="legendGroup"><summary>[[msg:Room types]]</summary><div id="legend"></div></details>
    </section>
  </aside>
</main>
"""

_STYLE = r"""
  :root {color-scheme:light;--ink:#19212e;--muted:#586575;--line:#d9dee6;--blue:#244edc;--sheet:#fff;font:14px/1.45 "Work Sans","Segoe UI","Microsoft YaHei UI","PingFang SC",sans-serif;color:var(--ink);background:white;}
  * {box-sizing:border-box;}
  [hidden] {display:none!important;}
  html,body {height:100%;margin:0;}
  body {display:flex;flex-direction:column;overflow:hidden;}
  button,select,input {font:inherit;}
  button,select {color:var(--ink);}
  button,select,summary,input[type=checkbox] {cursor:pointer;}
  :focus-visible {outline:3px solid #7190ef;outline-offset:2px;}
  h1,h2,p {margin:0;}
  .masthead {display:flex;align-items:center;justify-content:space-between;gap:14px;flex:none;height:64px;padding:0 clamp(16px,2.1vw,34px);border-bottom:1px solid var(--line);background:white;}
  h1 {display:flex;align-items:center;gap:10px;font-size:25px;letter-spacing:-.05em;line-height:1;font-weight:720;}
  .brand-mark {height:28px;width:28px;color:var(--blue);}
  .head-actions,.tool-row,.view-filters {display:flex;align-items:center;gap:8px;}
  .head-actions {gap:14px;}
  button,select {border:1px solid #bdc7d3;border-radius:5px;background:#fff;}
  button {display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:8px 12px;font-weight:570;white-space:nowrap;}
  button svg {display:block;width:17px;height:17px;fill:none;stroke:currentColor;stroke-width:1.65;stroke-linecap:round;stroke-linejoin:round;}
  button:hover,summary:hover {background:#f1f4f8;}
  select {padding:7px 8px;}
  #locale {border-color:transparent;max-width:106px;}
  .viewer {display:grid;grid-template-columns:minmax(0,1fr) clamp(310px,25vw,405px);flex:1;min-height:0;}
  .stage {position:relative;isolation:isolate;min-width:0;min-height:0;overflow:hidden;background:#f4f6f8;}
  #app {position:absolute;inset:0;}
  #app canvas {display:block;width:100%;height:100%;touch-action:none;}
  .compass {position:absolute;right:clamp(12px,1.7vw,26px);top:clamp(12px,1.7vw,24px);display:grid;justify-items:center;gap:1px;pointer-events:none;color:#546479;font:600 10px/1 "Segoe UI",sans-serif;}
  .compass svg {width:28px;height:28px;border:1px solid #cad2dd;border-radius:50%;background:#ffffffa8;}
  #northArrow {transform-origin:12px 12px;}
  .view-filters {position:absolute;left:clamp(12px,1.7vw,26px);top:clamp(12px,1.7vw,24px);align-items:stretch;max-width:calc(100% - 24px);}
  .filter {min-width:0;display:flex;align-items:center;gap:5px;background:#fff;border:1px solid #c6cfda;border-radius:5px;padding-left:10px;box-shadow:0 1px 3px #2236550c;}
  .filter>span {white-space:nowrap;font-size:12px;color:var(--muted);}
  .filter select {background:transparent;border:0;font-weight:570;min-width:0;max-width:clamp(105px,14vw,220px);font-size:13px;}
  .stage-tools {position:absolute;left:clamp(12px,1.7vw,26px);bottom:clamp(12px,1.7vw,24px);display:grid;justify-items:start;gap:9px;max-width:calc(100% - 24px);}
  .tool-row {flex-wrap:wrap;gap:6px;}
  .stage-tools button {font-size:13px;box-shadow:0 2px 5px #2236550e;}
  #meas,#sel {display:none;}
  #clearMeasure {display:none;}
  .stage-tools:has(#meas[style*="block"]) #measure {border-color:var(--blue);color:var(--blue);background:#eff3ff;}
  .stage-tools:has(#meas[style*="block"]) #clearMeasure {display:inline-flex;}
  #meas {padding:6px 10px;border-radius:5px;background:#283858;color:#fff;font-size:12px;}
  #sel {position:absolute;bottom:clamp(12px,1.7vw,24px);right:clamp(12px,1.7vw,24px);width:min(290px,46%);max-height:37%;overflow:auto;padding:12px 15px;background:#fff;border:1px solid #bfcbda;border-radius:7px;box-shadow:0 5px 20px #22365516;}
  #sel .hh {font-size:14px;font-weight:650;margin-bottom:7px;}
  .kv {display:flex;align-items:baseline;justify-content:space-between;gap:12px;}
  .kv span {color:var(--muted);}
  .kv b {font-variant-numeric:tabular-nums;font-weight:600;}
  #sel .kv,#elementCounts .kv {padding:3px 0;font-size:13px;}
  #sel .kv b {min-width:0;overflow-wrap:anywhere;text-align:right;}
  #sel .kv span {flex:none;}
  #panel {min-width:0;min-height:0;overflow:auto;scrollbar-gutter:stable;border-left:1px solid var(--line);padding:clamp(18px,1.65vw,28px);background:var(--sheet);}
  #panel h2 {font-size:clamp(18px,1.4vw,23px);line-height:1.27;font-weight:670;letter-spacing:-.035em;}
  #counts {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));margin:16px 0 12px;}
  #counts .kv,#dimensions .kv {display:flex;gap:2px;align-items:flex-start;flex-direction:column;}
  #counts .kv {flex-direction:column-reverse;padding:10px 1px 11px;}
  #counts .kv:nth-child(2n) {border-left:1px solid var(--line);padding-left:18px;}
  #counts .kv:nth-child(-n+2) {border-bottom:1px solid var(--line);}
  #counts .kv b {font-size:clamp(22px,1.8vw,30px);line-height:1.08;letter-spacing:-.04em;font-weight:640;}
  #counts .kv span,#dimensions .kv span {font-size:13px;}
  #dimensions {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));padding:12px 0;border-block:1px solid var(--line);}
  #dimensions .kv {gap:3px;}
  #dimensions .kv + .kv {padding-left:10px;border-left:1px solid var(--line);}
  #dimensions b {font-size:14px;white-space:nowrap;}
  .settings {margin-top:22px;}
  .settings h2 {margin-bottom:10px!important;}
  #panel details {border-bottom:1px solid var(--line);}
  #panel summary {position:relative;padding:11px 2px 11px 18px;font-size:14px;font-weight:590;list-style:none;cursor:pointer;border-radius:2px;}
  #panel summary::-webkit-details-marker {display:none;}
  #panel summary:before {content:"";position:absolute;top:calc(50% - 4px);left:3px;width:6px;height:6px;border-right:1.5px solid #5b687c;border-bottom:1.5px solid #5b687c;transform:rotate(-45deg);}
  #panel details[open]>summary:before {transform:rotate(45deg);}
  #elementCounts,.checks,#legend,#sections {padding:0 1px 12px;}
  .parameter {display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:4px 10px;margin:2px 0 13px;}
  .parameter label,.checks,.parameter select,#meshMode {font-size:14px;}
  .sec {font-size:13px;}
  .parameter output {font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums;}
  .parameter input,.parameter select {grid-column:1/-1;}
  input[type=range] {width:100%;accent-color:var(--blue);height:20px;margin:3px 0;}
  .parameter input[type=range] {appearance:none;background:transparent;cursor:pointer;}
  .parameter input[type=range]::-webkit-slider-runnable-track {height:4px;border-radius:4px;background:linear-gradient(to right,var(--blue) var(--progress),#d9dfe7 var(--progress));}
  .parameter input[type=range]::-webkit-slider-thumb {appearance:none;width:14px;height:14px;margin-top:-5px;border-radius:50%;background:var(--blue);}
  .parameter input[type=range]::-moz-range-track {height:4px;border-radius:4px;background:#d9dfe7;}
  .parameter input[type=range]::-moz-range-progress {height:4px;background:var(--blue);}
  .parameter input[type=range]::-moz-range-thumb {width:14px;height:14px;border:0;border-radius:50%;background:var(--blue);}
  input[type=checkbox] {width:15px;height:15px;accent-color:var(--blue);margin:0;flex:none;}
  .chk,.axis-label {display:flex;align-items:center;gap:8px;cursor:pointer;}
  .checks {display:grid;gap:9px;padding-top:1px;}
  .sec {display:grid;grid-template-columns:34px minmax(0,1fr) auto;align-items:center;gap:8px;padding:4px 0;}
  .sec .flip {font-size:12px;}
  #meshMode {width:100%;margin:0 0 13px;}
  #meshStatus {color:var(--muted);font-size:12px;margin-bottom:12px;}
  #meshStatus:empty {display:none;}
  #legend {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;}
  .lg {display:flex;align-items:center;gap:7px;font-size:12px;overflow-wrap:anywhere;}
  .sw {display:inline-block;width:12px;height:12px;flex:none;border-radius:3px;border:1px solid #0002;}
  @media (max-width:800px) and (orientation:portrait), (max-width:650px) {
    html,body {height:auto;min-height:100%;}
    body {overflow:auto;}
    .masthead {height:54px;padding:0 13px;}
    .head-actions {gap:3px;}
    .brand-mark {width:23px;height:23px;}
    h1 {font-size:21px;gap:6px;}
    .head-actions button {padding:7px 9px;font-size:12px;}
    .viewer {display:block;}
    .stage {height:60vh;height:60svh;min-height:300px;max-height:640px;}
    #panel {overflow:visible;scrollbar-gutter:auto;border-left:none;border-top:1px solid var(--line);padding:18px 20px 26px;}
    #counts {grid-template-columns:repeat(4,minmax(0,1fr));margin:12px 0 10px;}
    #counts .kv,#counts .kv:nth-child(2n),#counts .kv:nth-child(-n+2) {padding:2px 3px 2px 8px;border:none;}
    #counts .kv:first-child {padding-left:0;}
    #counts .kv + .kv {border-left:1px solid var(--line);}
    #counts .kv span {font-size:11px;}
    .view-filters {gap:5px;}
    .filter {padding-left:6px;gap:1px;}
    .filter>span {font-size:11px;}
    .filter select {font-size:12px;padding:6px 3px;max-width:30vw;}
    #sel {bottom:55px;width:min(260px,72%);padding:9px 12px;}
  }
  @media (max-height:530px) and (min-width:651px) { .masthead {height:46px;} }
"""

_HTML = r"""<!doctype html>
<html lang="__LOCALE__"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sim-BIM</title>
<style>__STYLE__</style></head>
<body>
__PANEL__
<script>__THREE_JS__</script>
<script>__ORBIT_JS__</script>
<script>window.GEO = __GEO_JSON__;</script>
<script>__APP_JS__</script>
</body></html>
"""


def app_js(locale: str = "zh-CN") -> str:
    """The viewer app script (for embedding + standalone syntax checking)."""
    return viewer_script(_APP_JS)


def discover_roles(bg_path: Path) -> dict:
    """Read room-use colours from this display geometry's own zone metadata."""
    try:
        geometry = json.loads(bg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(row["name"]): str(row["role"])
        for row in geometry.get("zone_meta", [])
        if row.get("name") and row.get("role")
    }


def wall_properties(source: dict) -> dict:
    """Project optional thickness evidence for selection, including unknowns.

    Component edits and reading evidence can coexist. If they disagree, expose
    that conflict instead of silently choosing one value in the viewer.
    """
    result = {}
    records = [*source.get("component_attributes", []), *source.get("wall_references", [])]
    for boundary in source.get("boundaries", []):
        if boundary.get("geometry_type") != "wall" or boundary.get("kind") != "physical":
            continue
        rows = [row for row in records
                if boundary["id"] in row.get("boundary_ids", [row.get("boundary_id")])]
        values = {row["thickness_m"] for row in rows if row.get("thickness_m") is not None}
        statuses = {row["evidence_status"] for row in rows if row.get("evidence_status")}
        sides = {row["reference_side"] for row in rows if row.get("reference_side")}
        result[boundary["id"]] = {
            "thickness_m": next(iter(values)) if len(values) == 1 else None,
            "conflict": len(values) > 1,
            "evidence_status": next(iter(statuses)) if len(statuses) == 1 else ("unknown" if not values else None),
            "reference_side": next(iter(sides)) if len(sides) == 1 else "unknown",
            "basis": list(dict.fromkeys(row.get("basis") or row.get("reference_basis")
                                        for row in rows if row.get("basis") or row.get("reference_basis"))),
            "source_refs": list(dict.fromkeys(ref for row in rows for ref in row.get("source_refs", []))),
        }
    return result


def build_viewer_html(data: dict, *, title: str | None = None, roles: dict | None = None,
                      mesh_path: Path | None = None, locale: str = "zh-CN") -> str:
    three_js = (_VENDOR / "three.min.js").read_text(encoding="utf-8")
    orbit_js = (_VENDOR / "OrbitControls.js").read_text(encoding="utf-8")
    from src.agent.geometry.openings import visible_wall_parts

    geo = {
        "locale": "en",
        "messages": viewer_messages(_HTML, _PANEL_HTML, _APP_JS),
        "zones": data.get("zones", []),
        "surfaces": data.get("surfaces", []),
        "windows": data.get("windows", []),
        "openings": data.get("openings", []),
        "visible_wall_parts": data["display_surface_parts"] if "display_surface_parts" in data else visible_wall_parts(data),
        "enclosure_regions": data.get("enclosure_regions", []),
        "roles": dict(roles if roles is not None else data.get("roles", {})),
        "source_model": data.get("source_model"),
    }
    from src.agent.roles import ROOM_TYPES
    from src.agent.geometry.source_naming import viewer_names
    geo["room_types"] = room_type_labels(ROOM_TYPES)
    geo["public_names"] = viewer_names(data, geo["visible_wall_parts"])
    source = data.get("source_model") or {}
    if mesh_path is not None:
        from src.agent.geometry.mesh_observation import MeshObservation
        geo["textured_mesh"] = MeshObservation(mesh_path).viewer_payload(source.get("mesh_frame"))
    geo["wall_properties"] = wall_properties(source)
    spaces = {row["id"]: row for row in source.get("spaces", [])}
    source_floors = {row["id"]: row for row in source.get("floors", [])}
    zone_map = source.get("derived", {}).get("zones", {})
    named_floors = geo["public_names"]["floors"]
    memberships = {}
    room_names = {}
    for zone in geo["zones"]:
        sid = zone_map.get(zone, zone)
        space = spaces.get(sid, {})
        role = geo["roles"].get(zone) or space.get("role")
        if role:
            geo["roles"][zone] = room_role(role)
        authored = space.get("display_name") or space.get("name") or space.get("label")
        if not authored and not space and re.search(r"[\s\u3400-\u9fff]", zone):
            authored = zone
        if authored and not re.search(r"(?:^[ZS]\d+(?:$|[_:/.-])|[_:]|/(?:wall|roof|floor|boundary)/)", str(authored), re.I):
            room_names[zone] = {"name": authored, "role": room_role(authored)}
        if not space:
            continue
        indices = [i for i, floor in enumerate(named_floors)
                   if space["floor_id"] == floor["id"]
                   or sid in source_floors.get(floor["id"], {}).get("spanning_space_ids", [])]
        if indices:
            memberships[zone] = indices
    geo["room_names"] = room_names
    geo["floor_memberships"] = memberships
    # Replace placeholders in the trusted template once, so an input name or
    # evidence containing another placeholder is preserved literally.
    values = {
        "LOCALE": "en", "STYLE": _STYLE,
        "THREE_JS": three_js, "ORBIT_JS": orbit_js, "GEO_JSON": _js_embed(geo),
        "APP_JS": app_js(),
    }
    template = _HTML.replace("__PANEL__", _PANEL_HTML)
    return re.sub(r"__([A-Z_]+)__", lambda m: values[m[1]], viewer_markup(template, geo["messages"]))


def load_viewer_geometry(path: Path) -> dict:
    """Load either a display geometry file or an authoritative source BIM.

    Source BIM inputs go through their validated display projection.  Unknown
    source schema versions fail rather than opening a plausible-looking empty
    viewer.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    schema = data.get("schema_version")
    if schema in {"source_bim_v2", "source_bim_v3"}:
        from src.agent.geometry.source_bim import source_view_geometry

        return source_view_geometry(data)
    if isinstance(schema, str) and schema.startswith("source_bim"):
        raise ValueError(f"unsupported source BIM schema: {schema}")

    source_path = path.with_name("source_model.json")
    if source_path.exists() and not data.get("source_model"):
        from src.agent.geometry.source_model import _digest

        source = json.loads(source_path.read_text(encoding="utf-8"))
        # Attach a source only when its digest binds this display geometry.
        if source.get("derived_geometry_sha256") == _digest(data):
            data["source_model"] = source
    return data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("json", help="display/building geometry JSON, or validated source_bim_v2/v3 source_model.json")
    ap.add_argument("--out", help="output HTML (default: <json dir>/geometry_viewer.html)")
    ap.add_argument("--title", default="")
    ap.add_argument("--locale", choices=("zh-CN", "en"), default="zh-CN")
    ap.add_argument("--mesh", type=Path, help="admitted original GLB; requires a matching source mesh_frame")
    ap.add_argument("--roles", help="optional JSON {zone: role} to colour zones by room type; "
                                    "default reads roles from the display geometry zone_meta")
    args = ap.parse_args()
    j = Path(args.json)
    data = load_viewer_geometry(j)
    if args.roles:
        roles = json.loads(Path(args.roles).read_text(encoding="utf-8"))
    else:
        roles = discover_roles(j) or None
    out = Path(args.out) if args.out else j.with_name("geometry_viewer.html")
    title = args.title or j.parent.parent.name or j.stem
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_viewer_html(data, title=title, roles=roles, mesh_path=args.mesh, locale=args.locale), encoding="utf-8")
    if roles:
        print(f"  room-type roles: {len(roles)} zones (colour-by-type enabled)")
    kb = out.stat().st_size // 1024
    print(f"wrote {out}  ({kb} KB, offline; orbit / opacity / sections / explode / vertex-measure)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
