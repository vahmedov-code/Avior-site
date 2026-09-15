/* Decorative PCB traces. No libraries or network requests. */
(() => {
  const hero = document.querySelector('.hero');
  if (!hero) return;
  const canvas = document.createElement('canvas');
  canvas.className = 'circuit-bg';
  canvas.setAttribute('aria-hidden', 'true');
  hero.prepend(canvas);
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let width, height, tracks = [], frame = 0, visible = true, copper, gold;
  const paths = [
    [[0,.13],[.15,.13],[.19,.21],[.34,.21]],
    [[0,.38],[.06,.38],[.11,.48],[.28,.48]],
    [[0,.78],[.13,.78],[.18,.68],[.37,.68]],
    [[.07,1],[.07,.92],[.16,.75],[.33,.75]],
    [[.35,0],[.35,.08],[.43,.23],[.58,.23]],
    [[.53,1],[.53,.85],[.62,.68],[.76,.68]],
    [[1,.12],[.9,.12],[.83,.26],[.69,.26]],
    [[1,.42],[.94,.42],[.88,.54],[.74,.54]],
    [[1,.85],[.87,.85],[.82,.75],[.67,.75]],
    [[.88,1],[.88,.94],[.79,.77],[.62,.77]],
    [[.72,0],[.72,.09],[.65,.22],[.51,.22]],
    [[0,.57],[.09,.57],[.14,.67],[.25,.67]]
  ];
  function resize() {
    width=hero.clientWidth; height=hero.clientHeight;
    const dpr=Math.min(devicePixelRatio || 1, 2);
    canvas.width=width*dpr; canvas.height=height*dpr;
    ctx.setTransform(dpr,0,0,dpr,0,0);
    const css=getComputedStyle(document.documentElement);
    copper=css.getPropertyValue('--copper').trim(); gold=css.getPropertyValue('--gold').trim();
    tracks=paths.map((points,i)=>{
      const pts=points.map(([x,y])=>[x*width,y*height]);
      const lengths=pts.slice(1).map((p,j)=>Math.hypot(p[0]-pts[j][0],p[1]-pts[j][1]));
      return {pts,lengths,total:lengths.reduce((a,b)=>a+b,0),offset:i*.173};
    });
    draw(0);
  }
  function pointAt(track,distance){
    for(let i=0;i<track.lengths.length;i++){
      if(distance<=track.lengths[i]){
        const t=distance/track.lengths[i];
        return [track.pts[i][0]+(track.pts[i+1][0]-track.pts[i][0])*t,track.pts[i][1]+(track.pts[i+1][1]-track.pts[i][1])*t];
      }
      distance-=track.lengths[i];
    }
    return track.pts[track.pts.length-1];
  }
  function draw(time){
    ctx.clearRect(0,0,width,height);
    tracks.forEach(track=>{
      ctx.globalAlpha=.21;ctx.strokeStyle=copper;ctx.lineWidth=1;
      ctx.beginPath();track.pts.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();
      const end=track.pts[track.pts.length-1];
      ctx.beginPath();ctx.arc(...end,3.5,0,Math.PI*2);ctx.stroke();
      if(motion.matches) return;
      const phase=(time/9000+track.offset)%1;
      if(phase>.8) return;
      const distance=(phase/.8)*track.total;
      ctx.strokeStyle=gold;ctx.lineWidth=2;
      for(let n=0;n<14;n++){
        const a=distance-n*3,b=a-3;if(b<0)continue;
        ctx.globalAlpha=(1-n/14)*.65;
        ctx.beginPath();ctx.moveTo(...pointAt(track,b));ctx.lineTo(...pointAt(track,a));ctx.stroke();
      }
      const p=pointAt(track,distance);ctx.globalAlpha=.9;ctx.fillStyle=gold;
      ctx.shadowColor=gold;ctx.shadowBlur=9;ctx.beginPath();ctx.arc(...p,1.7,0,Math.PI*2);ctx.fill();ctx.shadowBlur=0;
    });
    ctx.globalAlpha=1;
  }
  function tick(time){draw(time);frame=requestAnimationFrame(tick);}
  function sync(){cancelAnimationFrame(frame);if(visible&&!document.hidden&&!motion.matches)frame=requestAnimationFrame(tick);else draw(0);}
  new ResizeObserver(resize).observe(hero);
  new IntersectionObserver(entries=>{visible=entries[0].isIntersecting;sync();}).observe(hero);
  new MutationObserver(resize).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});
  document.addEventListener('visibilitychange',sync);motion.addEventListener('change',sync);
  resize();sync();
})();
