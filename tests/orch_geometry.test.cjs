const {test} = require('node:test');
const assert = require('node:assert/strict');
const {layout,route,routeGraph,blocked,wirePath} = require('../frontend/static/workbench/geometry.js');
const nodes = ['a','b','c','d','e','orphan'].map(id => ({id,w:240,h:id==='c'?600:160}));
const edges = [['a','b'],['a','c'],['b','d'],['c','d'],['d','e']];
test('variable-size branches and disconnected components never overlap; layout is deterministic', () => {
  const p=layout(nodes,edges); assert.deepEqual(p,layout(nodes,edges));
  for(const [a,b] of edges) assert.ok(p[b].x>p[a].x);
  const boxes=nodes.map(n=>({...n,...p[n.id]}));
  for(let i=0;i<boxes.length;i++) for(let j=i+1;j<boxes.length;j++) {
    const a=boxes[i],b=boxes[j];
    assert.ok(a.x+a.w<=b.x||b.x+b.w<=a.x||a.y+a.h<=b.y||b.y+b.h<=a.y);
  }
});
test('cycles fail without partially mutating the layout input', () => {
  const before=JSON.stringify(nodes); assert.throws(()=>layout(nodes,[...edges,['e','a']]));
  assert.equal(JSON.stringify(nodes),before); assert.deepEqual(layout([],[]),{});
});
test('compact layout folds long chains into non-overlapping rows', () => {
  const ns=Array.from({length:11},(_,i)=>({id:String(i),w:240,h:160}));
  const es=ns.slice(1).map((n,i)=>[String(i),n.id]);
  const p=layout(ns,es,120,72,4);
  assert.equal(new Set(Object.values(p).map(p=>p.y)).size,3);
  assert.ok(Math.max(...Object.values(p).map(p=>p.x))<1400);
  for(let i=0;i<ns.length;i++) for(let j=i+1;j<ns.length;j++) {
    const a=p[ns[i].id],b=p[ns[j].id];
    assert.ok(Math.abs(a.x-b.x)>=240||Math.abs(a.y-b.y)>=160);
  }
});
test('forward skip edges, branches and back edges route around every card', () => {
  const rects=[{x:100,y:100,w:240,h:160},{x:460,y:100,w:460,h:700},{x:1040,y:100,w:240,h:160}];
  for (const [start,end] of [[{x:340,y:180},{x:1040,y:180}], [{x:1280,y:180},{x:100,y:180}]]) {
    const r=route(start,end,rects,2);
    assert.deepEqual(r.points[0],start); assert.deepEqual(r.points.at(-1),end);
    for(let i=1;i<r.points.length;i++) {
      const a=r.points[i-1],b=r.points[i];
      assert.ok(a.x===b.x||a.y===b.y); assert.ok(!blocked(a,b,rects));
    }
  }
});

test('clear forward wires are cubic with centered labels and unchanged anchors', () => {
  const start={x:340,y:180}, end={x:520,y:400};
  const r=route(start,end,[{x:100,y:100,w:240,h:160},{x:520,y:320,w:240,h:160}]);
  assert.match(r.d, /^M 340 180 C /);
  assert.ok(r.d.endsWith('520 400'));
  assert.equal(r.labelX,430); assert.equal(r.labelY,278);
  assert.deepEqual(r.points[0],start); assert.deepEqual(r.points.at(-1),end);
});

// Sample the displayed path, not just the orthogonal routing skeleton.
function samples(d) {
  const tokens=d.match(/[MLQC]|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi);
  const result=[]; let p={x:0,y:0}, i=0;
  const point=()=>({x:Number(tokens[i++]),y:Number(tokens[i++])});
  while(i<tokens.length) {
    const cmd=tokens[i++];
    if(cmd==='M') {p=point();result.push(p);continue;}
    const a=p, b=point(), c=cmd==='Q'||cmd==='C'?point():null, e=cmd==='C'?point():null;
    for(let step=1;step<=200;step++) {
      const t=step/200,u=1-t;
      result.push(Object.fromEntries(['x','y'].map(k=>[k,cmd==='L'?u*a[k]+t*b[k]:cmd==='Q'?u*u*a[k]+2*u*t*b[k]+t*t*c[k]:u*u*u*a[k]+3*u*u*t*b[k]+3*u*t*t*c[k]+t*t*t*e[k]])));
    }
    p=e||c||b;
  }
  return result;
}

test('rounded obstacle detours and loop wires never cut through cards', () => {
  const rects=[{x:100,y:100,w:240,h:160},{x:460,y:100,w:460,h:700},{x:1040,y:100,w:240,h:160}];
  for(const [start,end] of [[{x:340,y:180},{x:1040,y:180}],[{x:1280,y:180},{x:100,y:180}]]) {
    const r=route(start,end,rects,2);
    assert.match(r.d,/ Q /); assert.ok(!r.d.includes(' C '));
    for(const p of samples(r.d)) for(const b of rects) {
      assert.ok(!(p.x>b.x+.001&&p.x<b.x+b.w-.001&&p.y>b.y+.001&&p.y<b.y+b.h-.001),JSON.stringify(p));
    }
  }
});

test('wire skin handles empty, coincident and short paths without invalid coordinates', () => {
  assert.equal(wirePath([]).d,'');
  for(const pts of [[{x:1,y:2}],[{x:1,y:2},{x:1,y:2}],[{x:0,y:0},{x:5,y:0},{x:5,y:4},{x:10,y:4}]]) {
    const before=JSON.stringify(pts),d=wirePath(pts).d;
    assert.ok(!/NaN|Infinity/.test(d)); assert.equal(JSON.stringify(pts),before);
  }
});

test('multiple returns stay on separate outside channels without curls or card intersections', () => {
  const rects=[{x:100,y:100,w:240,h:160},{x:460,y:100,w:240,h:160},
    {x:820,y:100,w:240,h:160},{x:460,y:380,w:240,h:160}];
  const connections=[
    {key:'main',start:{x:340,y:149},end:{x:460,y:149}},
    {key:'return-a',start:{x:1060,y:149},end:{x:100,y:149},feedback:true},
    {key:'return-b',start:{x:700,y:429},end:{x:100,y:149},feedback:true},
    {key:'self',start:{x:700,y:173},end:{x:460,y:149},feedback:true},
  ];
  const routes=routeGraph(connections,rects);
  const returned=routes.filter(r=>r.feedback);
  assert.equal(new Set(returned.map(r=>r.channelY)).size,3);
  for (const r of returned) {
    assert.ok(r.channelY<100 || r.channelY>540);assert.ok(!r.d.includes(' C '));
    assert.ok(r.points[1].x>r.points[0].x);assert.ok(r.points.at(-2).x<r.points.at(-1).x);
    // One westbound span outside the graph, never a U-turn at either port.
    const west=r.points.slice(1).filter((p,i)=>p.x<r.points[i].x);
    assert.equal(west.length,1);assert.equal(west[0].y,r.channelY);
    for (const p of samples(r.d)) for (const b of rects) {
      assert.ok(!(p.x>b.x+.001&&p.x<b.x+b.w-.001&&p.y>b.y+.001&&p.y<b.y+b.h-.001));
    }
  }
  const reversed=routeGraph([...connections].reverse(),rects).reverse();
  assert.deepEqual(routes,reversed);
});

test('congested forward edges do not double back to avoid an ordinary crossing', () => {
  const rects=[{x:100,y:100,w:240,h:160},{x:820,y:380,w:240,h:160}];
  const occupied=Array.from({length:5},(_,i)=>[{x:410+i*50,y:0},{x:410+i*50,y:700}]);
  const r=route({x:340,y:149},{x:820,y:429},rects,0,occupied);
  assert.equal(r.feedback,false);
  assert.ok(r.points.every((p,i)=>!i || p.x>=r.points[i-1].x));
  assert.ok(r.points.every(p=>p.y>=149 && p.y<=429));
});
