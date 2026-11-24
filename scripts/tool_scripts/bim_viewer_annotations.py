"""Object annotations embedded in the standalone Sim-BIM viewer."""

ANNOTATION_MARKUP = r"""
    <div id="annotationLayer" hidden></div>
    <form id="annotationEditor" role="dialog" aria-labelledby="annotationTarget" hidden>
      <div class="annotation-heading">
        <strong id="annotationTarget"></strong>
        <button type="button" id="deleteAnnotation" hidden>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M7 7l1 13h8l1-13M10 10v7m4-7v7"/></svg>
        </button>
      </div>
      <div class="annotation-input">
        <textarea id="annotationText" rows="2"></textarea>
        <button type="submit" id="confirmAnnotation" disabled><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m4 12 5 5L20 6"/></svg></button>
      </div>
    </form>
"""

ANNOTATION_CSS = r"""
  #annotationLayer {position:absolute;inset:0;pointer-events:none;overflow:hidden;}
  .annotation-pin {position:absolute;pointer-events:auto;display:flex;align-items:center;gap:6px;max-width:min(200px,26%);
    transform:translate(-5px,-50%);padding:4px 8px 4px 4px;border:1px solid #e5b7ba;border-radius:5px;
    background:#fff;color:#62232a;font-size:12px;line-height:1.4;text-align:left;box-shadow:0 2px 6px #45151918;}
  .annotation-pin::before {content:"";flex:none;width:7px;height:7px;border-radius:50%;background:#d52f43;}
  .annotation-pin span {overflow:hidden;text-overflow:ellipsis;}
  .annotation-pin.flip:not(.compact) {transform:translate(calc(-100% + 5px),-50%);flex-direction:row-reverse;padding:4px 4px 4px 8px;}
  .annotation-pin.compact {padding:4px;border-radius:50%;transform:translate(-50%,-50%);}
  .annotation-pin.compact span {display:none;}
  .annotation-pin:focus-visible,.annotation-pin:hover {outline:2px solid #d54555;outline-offset:1px;z-index:1;background:#fff7f7;}
  #annotationEditor {position:absolute;z-index:3;width:min(242px,calc(100% - 20px));box-sizing:border-box;
    padding:9px 11px 11px;background:#fff;border:1px solid #e4a7ae;border-radius:8px;
    color:#202b3d;box-shadow:0 5px 19px #3e213126;}
  .annotation-heading,.annotation-input {display:flex;align-items:center;gap:8px;}
  .annotation-heading {justify-content:space-between;min-height:20px;margin-bottom:6px;}
  #annotationTarget {overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;line-height:1.4;}
  #annotationText {box-sizing:border-box;min-width:0;flex:1;resize:vertical;min-height:46px;max-height:22vh;
    font:inherit;font-size:13px;line-height:1.45;color:inherit;padding:5px 7px;border:1px solid #bac5d1;border-radius:4px;}
  #deleteAnnotation,#confirmAnnotation {width:28px;height:28px;padding:3px;flex:none;}
  #deleteAnnotation {border:0;color:#7c5d65;background:transparent;}
  #confirmAnnotation {color:#fff;background:#a82639;border-color:#a82639;}
  #confirmAnnotation:disabled {background:#e5dce0;border-color:#e5dce0;color:#7b7076;}
"""

ANNOTATION_JS = r"""
  const editMarkers=(()=>{
    const layer=$('annotationLayer'), form=$('annotationEditor'), input=$('annotationText');
    const heading=$('annotationTarget'), confirm=$('confirmAnnotation'), remove=$('deleteAnnotation');
    const highlighted=new Set(), byTarget=new Map(), byAnchor=new Map();
    const pins=[], projected=new THREE.Vector3();
    let targets=[], editingId=null, indexed=false;
    let renderedNotes=null, renderedView=null, renderedLocale=null;
    const localized=(en,zh)=>locale==='en' ? en : zh;
    const notes=()=>viewerDraft.state.notes;
    const faceName=u=>String(u.publicName || u.name || u.boundaryId || u.zone);
    const targetKey=(kind,id,surface)=>JSON.stringify(kind==='surface' ? [kind,id,surface] : [kind,id]);
    const key=t=>targetKey(t.kind,t.id,t.surface);
    const floorId=i=>String(NAMED_FLOORS[i]?.id ?? i);
    const anchorKey=(zone,surface)=>JSON.stringify([zone,surface]);
    function addIndex(map,id,mesh){
      if(!map.has(id)) map.set(id,[]);
      map.get(id).push(mesh);
    }
    function indexMeshes(){
      if(indexed) return;
      for(const mesh of allPickables()){
        const u=mesh.userData, surface=faceName(u);
        addIndex(byAnchor,anchorKey(String(u.zone),surface),mesh);
        addIndex(byTarget,targetKey('surface',String(surfaceBinding(u)[1]),surface),mesh);
        addIndex(byTarget,targetKey('zone',String(sourceIdOfZone(u.zone))),mesh);
        for(const floor of actualStoreys(u.zone,u.floor)){
          addIndex(byTarget,targetKey('floor',floorId(floor)),mesh);
        }
      }
      indexed=true;
    }
    function targetAt(hit,kind){
      const mesh=hit.object,u=mesh.userData,surface=faceName(u);
      let id,label;
      if(kind==='floor'){
        const floor=pickedFloor(hit);
        id=floorId(floor);label=floorLabel(floor);
      }else if(kind==='zone'){
        id=sourceIdOfZone(u.zone);label=spaceName(u.zone);
      }else{
        id=surfaceBinding(u)[1];label=surface;
      }
      return {kind,id:String(id),label:String(label),zone:String(u.zone),surface,
        point:hit.point.clone().sub(mesh.position).toArray()};
    }
    function recolor(){
      highlighted.clear();
      function include(t){for(const mesh of byTarget.get(key(t)) || []) highlighted.add(mesh);}
      if(editView){
        for(const note of notes()) for(const target of note.targets) include(target);
        targets.forEach(include);
      }
      refreshColors();
    }
    function pending(){
      if(!targets.length) return false;
      if(!editingId) return Boolean(input.value.trim());
      const saved=notes().find(n=>n.id===editingId);
      return !!saved && (input.value.trim()!==saved.text || targets.length!==saved.targets.length ||
        targets.some((t,i)=>key(t)!==key(saved.targets[i])));
    }
    function editorLabels(){
      const names=targets.map(t=>t.label);
      heading.textContent=names.length===1 ? names[0] :
        localized(names.length+' objects selected','已选 '+names.length+' 个构件');
      heading.title=names.join(' · ');
      input.placeholder=localized('Describe the change…','描述要做的修改…');
      input.setAttribute('aria-label',localized('Object note','构件批注'));
      for(const [button,label] of [[confirm,localized('Confirm note','确认批注')],[remove,localized('Delete note','删除批注')]]){
        button.title=label;button.setAttribute('aria-label',label);
      }
      confirm.disabled=!input.value.trim();remove.hidden=!editingId;
      form.hidden=!editView || !targets.length;
    }
    function pickTarget(hit,kind,extend){
      if(!hit || !['floor','zone','surface'].includes(kind)){cancel();return;}
      const target=targetAt(hit,kind), i=targets.findIndex(t=>key(t)===key(target));
      if(extend){
        if(i<0) targets.push(target);
        else targets.splice(i,1);
      }else if(i===0 && targets.length===1){
        targets[0]=target;
      }else{
        editingId=null;input.value='';targets=[target];
      }
      if(!targets.length){cancel();return;}
      sync(true);input.focus({preventScroll:true});
    }
    function open(note){
      editingId=note.id;targets=note.targets.slice();input.value=note.text;
      sync(true);input.focus({preventScroll:true});
    }
    function cancel(){
      editingId=null;targets=[];input.value='';
      $('editHint').hidden=true;
      sync(true);
    }
    form.onsubmit=event=>{
      event.preventDefault();
      const text=input.value.trim();
      if(!editView || !text || !targets.length) return;
      const id=editingId || (globalThis.crypto?.randomUUID?.() ?? String(Date.now())+'-'+Math.random().toString(36).slice(2));
      const note={id,text,targets:targets.slice()};
      const next=editingId ? notes().map(n=>n.id===id ? note : n) : [...notes(),note];
      viewerDraft.update({notes:next});
      cancel();
    };
    remove.onclick=()=>{
      if(!editingId) return;
      viewerDraft.update({notes:notes().filter(note=>note.id!==editingId)});
      cancel();
    };
    input.oninput=()=>{
      confirm.disabled=!input.value.trim();
      if(!pending()) $('editHint').hidden=true;
    };
    window.addEventListener('keydown',event=>{
      if(!editView || !targets.length || event.isComposing) return;
      if(event.key==='Escape' && !document.querySelector('dialog[open]')){
        cancel();
      }
      if(event.target===input && event.key==='Enter' && (event.ctrlKey || event.metaKey)){
        event.preventDefault();if(!confirm.disabled) form.requestSubmit();
      }
    });
    function makePin(note,target){
      const el=document.createElement('button'), text=document.createElement('span');
      el.type='button';el.className='annotation-pin';el.hidden=true;el.dataset.noteId=note.id;
      el.title=target.label+' · '+note.text;el.setAttribute('aria-label',el.title);
      text.textContent=note.text;el.appendChild(text);el.onclick=()=>open(note);
      layer.appendChild(el);
      return {el,target};
    }
    function sync(force=false){
      const saved=notes(), snapshot=editView ? JSON.stringify(saved) : '';
      if(!force && renderedView===editView && renderedLocale===locale && renderedNotes===snapshot) return;
      renderedView=editView;renderedLocale=locale;renderedNotes=snapshot;
      if(editView) indexMeshes();
      layer.replaceChildren();pins.length=0;layer.hidden=!editView;
      if(editView){
        if(editingId && !saved.some(note=>note.id===editingId)) editingId=null;
        for(const note of saved){
          if(note.id!==editingId) pins.push(note.targets.map(target=>makePin(note,target)));
        }
      }
      editorLabels();recolor();frame();
    }
    function screenPoint(target,width,height){
      const faces=byAnchor.get(anchorKey(target.zone,target.surface));
      const mesh=faces?.find(m=>m.visible);
      if(!mesh) return null;
      projected.fromArray(target.point).add(mesh.position);
      if(comparisonPlanes().some(p=>p.distanceToPoint(projected)<-0.0001)) return null;
      projected.project(camera);
      if(Math.abs(projected.x)>1 || Math.abs(projected.y)>1 || Math.abs(projected.z)>1) return null;
      return {x:Math.round((projected.x+1)*width/2),y:Math.round((1-projected.y)*height/2)};
    }
    function place(el,x,y){
      const left=x+'px',top=y+'px';
      if(el.style.left!==left) el.style.left=left;
      if(el.style.top!==top) el.style.top=top;
    }
    function frame(){
      if(!editView || (!pins.length && !targets.length)) return;
      const width=viewport.clientWidth,height=viewport.clientHeight;
      const occupied=[];
      for(const group of pins){
        let labeled=false;
        for(const {el,target} of group){
          const point=screenPoint(target,width,height);
          if(el.hidden!==!point) el.hidden=!point;
          if(!point) continue;
          el.classList.toggle('compact',labeled);
          el.classList.toggle('flip',point.x>width-Math.min(200,width*.26));
          let y=point.y;
          if(!labeled){
            for(const prev of occupied){
              if(Math.abs(prev.y-y)<25 && Math.abs(prev.x-point.x)<128) y=prev.y+25;
            }
            occupied.push({x:point.x,y});
          }
          place(el,point.x,Math.min(height-13,y));labeled=true;
        }
      }
      if(!targets.length) return;
      const active=targets[targets.length-1],position=screenPoint(active,width,height);
      if(!position && form.style.left) return;
      const point=position || {x:width/2,y:height/2};
      const w=form.offsetWidth,h=form.offsetHeight,panel=$('editPanel');
      let x=point.x+w+16<width ? point.x+16 : point.x-w-16;
      const y=Math.max(8,Math.min(height-h-8,point.y+12));
      if(panel && !panel.hidden){
        const right=panel.offsetLeft+panel.offsetWidth, top=panel.offsetTop-panel.offsetHeight/2;
        if(x<right+8 && y+h>top && y<top+panel.offsetHeight) x=right+8;
      }
      place(form,Math.max(8,Math.min(width-w-8,x)),y);
    }
    return {highlighted,pickTarget,cancel,pending,sync,frame};
  })();
"""
