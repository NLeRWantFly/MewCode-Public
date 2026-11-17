// Reuses the original deterministic layout and obstacle-aware routing.
function layout(nodes, edges, gapX = 120, gapY = 72, maxColumns = Infinity) {
  const byId = new Map(nodes.map(n => [n.id, n]));
  const links = edges.filter(([a,b]) => a !== b && byId.has(a) && byId.has(b));
  const neighbors = new Map(nodes.map(n => [n.id, []]));
  for (const [a,b] of links) { neighbors.get(a).push(b); neighbors.get(b).push(a); }
  const visited = new Set(), components = [];
  for (const n of nodes) {
    if (visited.has(n.id)) continue;
    const component = [], queue = [n.id]; visited.add(n.id);
    while (queue.length) {
      const id = queue.shift(); component.push(id);
      for (const next of neighbors.get(id)) if (!visited.has(next)) { visited.add(next); queue.push(next); }
    }
    components.push(component);
  }
  components.sort((a,b) => b.length - a.length);
  const positions = {}; let offsetY = 100;
  for (const ids of components) {
    const live = new Set(ids), rank = new Map(ids.map(id => [id, 0]));
    const incoming = new Map(ids.map(id => [id, []])), outgoing = new Map(ids.map(id => [id, []]));
    for (const [a,b] of links) if (live.has(a)) { incoming.get(b).push(a); outgoing.get(a).push(b); }
    const degree = new Map(ids.map(id => [id, incoming.get(id).length]));
    const queue = ids.filter(id => !degree.get(id)); let count = 0;
    while (queue.length) {
      const id = queue.shift(); count++;
      for (const next of outgoing.get(id)) {
        rank.set(next, Math.max(rank.get(next), rank.get(id) + 1));
        degree.set(next, degree.get(next) - 1); if (!degree.get(next)) queue.push(next);
      }
    }
    if (count !== ids.length) throw new Error("存在未声明回边，请先标记循环再布局。");
    const columns = Array.from({length: Math.max(...rank.values()) + 1}, () => []);
    for (const id of ids) columns[rank.get(id)].push(id);
    const order = new Map(ids.map((id,i) => [id,i]));
    // Barycentric sweeps reduce crossings; stable tie order preserves branches.
    for (let sweep = 0; sweep < 4; sweep++) {
      const cols = sweep % 2 ? [...columns].reverse() : columns;
      const adjacent = sweep % 2 ? outgoing : incoming;
      for (const col of cols) {
        const score = id => {
          const ns = adjacent.get(id); return ns.length ? ns.reduce((s,n) => s + order.get(n), 0) / ns.length : order.get(id);
        };
        col.sort((a,b) => score(a) - score(b)); col.forEach((id,i) => order.set(id,i));
      }
    }
    const heights = columns.map(col => col.reduce((s,id) => s + byId.get(id).h, 0) + (col.length - 1) * gapY);
    const height = Math.max(...heights); let x = 120, rowY = offsetY;
    columns.forEach((col,i) => {
      if (i && i % maxColumns === 0) { x = 120; rowY += height + 180; }
      let y = rowY + (height - heights[i]) / 2;
      for (const id of col) { positions[id] = {x,y}; y += byId.get(id).h + gapY; }
      x += Math.max(...col.map(id => byId.get(id).w)) + gapX;
    });
    offsetY = rowY + height + 160;
  }
  return positions;
}

function blocked(a, b, boxes) {
  return boxes.some(r => a.x === b.x
    ? a.x > r.x && a.x < r.x+r.w && Math.max(a.y,b.y) > r.y && Math.min(a.y,b.y) < r.y+r.h
    : a.y > r.y && a.y < r.y+r.h && Math.max(a.x,b.x) > r.x && Math.min(a.x,b.x) < r.x+r.w);
}

// Presentation only: retain routing points for layout, bounds and congestion.
// A cubic stays inside its control-point rectangle. Use it only when that
// entire corridor is clear; obstacle detours retain their route with soft bends.
function wirePath(points, rects = []) {
  const start = points[0], end = points[points.length - 1];
  if (!start) return {d: "", curved: false};
  const top = Math.min(start.y, end.y), bottom = Math.max(start.y, end.y);
  const forward = end.x - start.x >= 48 && points.every((p, i) =>
    p.y >= top && p.y <= bottom && (!i || p.x >= points[i - 1].x));
  const clear = !rects.some(r => r.x < end.x && r.x + r.w > start.x &&
    r.y - 8 < bottom && r.y + r.h + 8 > top);
  if (forward && clear) {
    const pull = (end.x - start.x) * .5;
    return {d: `M ${start.x} ${start.y} C ${start.x + pull} ${start.y} ${end.x - pull} ${end.y} ${end.x} ${end.y}`, curved: true};
  }
  let d = `M ${start.x} ${start.y}`;
  for (let i = 1; i < points.length - 1; i++) {
    const a = points[i - 1], b = points[i], c = points[i + 1];
    const ab = Math.hypot(b.x - a.x, b.y - a.y), bc = Math.hypot(c.x - b.x, c.y - b.y);
    if (!ab || !bc) continue;
    // Smaller than routing clearance: rounding cannot cut into a card.
    const radius = Math.min(12, ab / 2, bc / 2);
    const before = {x: b.x + (a.x - b.x) * radius / ab, y: b.y + (a.y - b.y) * radius / ab};
    const after = {x: b.x + (c.x - b.x) * radius / bc, y: b.y + (c.y - b.y) * radius / bc};
    d += ` L ${before.x} ${before.y} Q ${b.x} ${b.y} ${after.x} ${after.y}`;
  }
  if (points.length > 1) d += ` L ${end.x} ${end.y}`;
  return {d, curved: false};
}

// Search includes the incoming direction: a short crossing is preferable to
// a large detour, and each additional bend has a cost. Ports always face right.
function findRoute(a, b, boxes, occupied, {forward=false, firstDirection=0, lastDirection=0}={}) {
  if ((a.x===b.x || a.y===b.y) && !blocked(a,b,boxes) &&
      (a.x===b.x || b.x>=a.x)) return [a,b];
  const xs = [...new Set([a.x,b.x,(a.x+b.x)/2,...boxes.flatMap(r => [r.x-8,r.x+r.w+8])])].sort((a,b) => a-b);
  const ys = [...new Set([a.y,b.y,...boxes.flatMap(r => [r.y-8,r.y+r.h+8])])].sort((a,b) => a-b);
  const key = (x,y,d) => (y*xs.length+x)*5+d;
  const first = key(xs.indexOf(a.x),ys.indexOf(a.y),firstDirection ?? 4);
  const distances = new Map([[first,0]]), previous = new Map(), heap = [];
  function push(item) {
    heap.push(item); let i=heap.length-1;
    while (i>0) { const p=(i-1)>>1; if (heap[p][0]<=item[0]) break; heap[i]=heap[p]; i=p; } heap[i]=item;
  }
  function pop() {
    const result=heap[0], tail=heap.pop(); if (!heap.length) return result;
    let i=0; while (i*2+1<heap.length) { let c=i*2+1; if (c+1<heap.length && heap[c+1][0]<heap[c][0]) c++;
      if (heap[c][0]>=tail[0]) break; heap[i]=heap[c]; i=c; } heap[i]=tail; return result;
  }
  push([0,first]); const closed = new Set(); let last;
  function congestion(p,q) {
    let cost = 0;
    for (const [u,v] of occupied) {
      const vertical = p.x===q.x, otherVertical=u.x===v.x;
      if(vertical!==otherVertical) {
        const h=vertical?[u,v]:[p,q], t=vertical?[p,q]:[u,v];
        const x=t[0].x,y=h[0].y;
        // Shared endpoint trunks are intentional, not a reason to circle a node.
        if(x>Math.min(h[0].x,h[1].x)&&x<Math.max(h[0].x,h[1].x)&&
           y>Math.min(t[0].y,t[1].y)&&y<Math.max(t[0].y,t[1].y)) cost+=80;
      } else if ((vertical&&p.x===u.x)||(!vertical&&p.y===u.y)) {
        const axis=vertical?'y':'x';
        const overlap=Math.min(Math.max(p[axis],q[axis]),Math.max(u[axis],v[axis]))-
          Math.max(Math.min(p[axis],q[axis]),Math.min(u[axis],v[axis]));
        if(overlap>0) cost+=Math.min(40,overlap*.15);
      }
    }
    return cost;
  }
  while (heap.length) {
    const [,current] = pop(); if (closed.has(current)) continue;
    const direction=current%5, vertex=Math.floor(current/5);
    const x=vertex%xs.length, y=Math.floor(vertex/xs.length), p={x:xs[x],y:ys[y]};
    if (p.x===b.x && p.y===b.y && (lastDirection==null || direction!==(lastDirection+2)%4)) {last=current;break;}
    closed.add(current);
    for (const [nx,ny,nextDirection] of [[x+1,y,0],[x,y+1,1],[x-1,y,2],[x,y-1,3]]) {
      if (nx<0||ny<0||nx>=xs.length||ny>=ys.length) continue;
      if (direction!==4 && nextDirection===(direction+2)%4 || forward && (nextDirection===2 || xs[nx]<a.x || xs[nx]>b.x)) continue;
      const next=key(nx,ny,nextDirection), q={x:xs[nx],y:ys[ny]};
      if (closed.has(next)||blocked(p,q,boxes)) continue;
      const distance=distances.get(current)+Math.abs(p.x-q.x)+Math.abs(p.y-q.y)+
        (direction!==4 && direction!==nextDirection ? 36 : 0)+congestion(p,q);
      if (distance >= (distances.get(next) ?? Infinity)) continue;
      distances.set(next,distance); previous.set(next,current);
      push([distance+Math.abs(q.x-b.x)+Math.abs(q.y-b.y),next]);
    }
  }
  if (last===undefined) return null;
  const path=[]; let current=last;
  while (current !== undefined) {
    const vertex=Math.floor(current/5);
    path.push({x:xs[vertex%xs.length],y:ys[Math.floor(vertex/xs.length)]});current=previous.get(current);
  }
  return path.reverse();
}

function route(start, end, rects, lane = 0, occupied = [], options = {}) {
  const margin=16, feedback=options.feedback ?? end.x<=start.x;
  const boxes=rects.map(r=>({x:r.x-margin,y:r.y-margin,w:r.w+2*margin,h:r.h+2*margin}));
  const collar=24+(feedback ? lane%4*10 : 0);
  const a={x:start.x+collar,y:start.y},b={x:end.x-collar,y:end.y};
  let points, channelY;
  if (feedback) {
    const top=Math.min(start.y,end.y,...rects.map(r=>r.y))-48-lane*24;
    const bottom=Math.max(start.y,end.y,...rects.map(r=>r.y+r.h))+48+lane*24;
    channelY=options.channelY ?? (start.y+end.y-top*2 < bottom*2-start.y-end.y ? top : bottom);
    const left=findRoute(a,{x:a.x,y:channelY},boxes,[],{lastDirection:null});
    const right=findRoute({x:b.x,y:channelY},b,boxes,[],{firstDirection:null});
    if (left && right) points=[start,...left,...right,end];
  } else {
    // Preserve left-to-right flow whenever an obstacle detour can do so.
    const path=findRoute(a,b,boxes,occupied,{forward:b.x>=a.x}) || findRoute(a,b,boxes,occupied);
    if (path) points=[start,...path,end];
  }
  if (!points) {
    // Enclosed ports on overlapping manual cards have no obstacle-free route.
    const y=channelY ?? Math.max(start.y,end.y,...boxes.map(r=>r.y+r.h))+40+lane*24;
    points=[start,a,{x:a.x,y},{x:b.x,y},b,end];
  }
  points=points.filter((p,i) => !i||p.x!==points[i-1].x||p.y!==points[i-1].y);
  for (let i=1;i<points.length-1;) {
    const [a,b,c]=[points[i-1],points[i],points[i+1]];
    if ((a.x===b.x&&b.x===c.x || a.y===b.y&&b.y===c.y) &&
        (b.x-a.x)*(c.x-b.x)+(b.y-a.y)*(c.y-b.y)>=0) points.splice(i,1); else i++;
  }
  let labelA=points[0],labelB=points[1],longest=0;
  for(let i=1;i<points.length;i++) {
    const p=points[i-1],q=points[i],length=Math.abs(p.x-q.x)+Math.abs(p.y-q.y);
    if(length>longest) {longest=length;labelA=p;labelB=q;}
  }
  const visual = wirePath(points, rects);
  return {points,d:visual.d,feedback,channelY,
    x1:start.x,y1:start.y,x2:end.x,y2:end.y,
    labelX:visual.curved ? (start.x+end.x)/2 : (labelA.x+labelB.x)/2,
    labelY:(visual.curved ? (start.y+end.y)/2 : (labelA.y+labelB.y)/2)-12};
}

// Route ordinary connections first. Returns get their own parallel channels
// beyond the cards; nested short returns stay closest to the main flow.
function routeGraph(connections, rects, {feedbackBounds=rects}={}) {
  const result=new Array(connections.length),occupied=[],lanes={top:0,bottom:0};
  const ordered=connections.map((edge,index)=>({...edge,index,feedback:edge.feedback || edge.end.x<=edge.start.x}));
  ordered.sort((a,b)=>Number(a.feedback)-Number(b.feedback) ||
    Math.abs(a.end.x-a.start.x)-Math.abs(b.end.x-b.start.x) ||
    a.start.y-b.start.y || a.end.y-b.end.y || String(a.key).localeCompare(String(b.key)));
  const top=Math.min(0,...feedbackBounds.map(r=>r.y))-48,bottom=Math.max(0,...feedbackBounds.map(r=>r.y+r.h))+48;
  for (const edge of ordered) {
    let lane=0,channelY;
    if (edge.feedback) {
      const upper=top-lanes.top*24,lower=bottom+lanes.bottom*24;
      const side=edge.start.y+edge.end.y-upper*2 < lower*2-edge.start.y-edge.end.y ? 'top' : 'bottom';
      lane=lanes[side]++;channelY=side==='top' ? upper : lower;
    }
    const wire=route(edge.start,edge.end,rects,lane,occupied,{feedback:edge.feedback,channelY});
    result[edge.index]=wire;occupied.push(...wire.points.slice(1).map((point,i)=>[wire.points[i],point]));
  }
  return result;
}
export {layout,route,routeGraph,blocked,wirePath};
