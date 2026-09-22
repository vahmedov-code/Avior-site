(() => {
 const input=document.querySelector('#blog-search'),results=document.querySelector('#blog-results'),pager=document.querySelector('#blog-pagination'),count=document.querySelector('#blog-count');
 if(!input||!results)return;
 const links=[...document.querySelectorAll('[data-category]')];let category=links.find(a=>a.hasAttribute('aria-current'))?.dataset.category||'',data=null,timer;
 const params=new URLSearchParams(location.search);input.value=params.get('q')||'';
 const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
 function render(page=1){
   if(!data)return;const q=input.value.trim().toLocaleLowerCase('ru');
   const words=q.split(/\s+/).filter(Boolean);
   const filtered=data.filter(p=>(!category||p.category===category)&&words.every(w=>(p.title+' '+p.excerpt+' '+p.text).toLocaleLowerCase('ru').includes(w)));
   results.replaceChildren();pager.replaceChildren();count.textContent='Найдено: '+filtered.length;
   filtered.slice((page-1)*6,page*6).forEach(p=>{
     const a=node('a','', 'blog-card');a.href='/blog/'+p.slug+'/';const img=node('img');img.src=p.cover;img.alt=p.alt;img.loading='lazy';a.append(img);
     const copy=node('div','', 'blog-card-copy');copy.append(node('span',p.category_label,'blog-kicker'),node('h2',p.title),node('p',p.excerpt));
     const meta=node('div','', 'blog-card-meta');const time=node('time',p.date.split('-').reverse().join('.'));time.dateTime=p.date;meta.append(time,node('span','Читать ↗'));copy.append(meta);a.append(copy);results.append(a);
   });
   if(!filtered.length){const empty=node('div','', 'blog-empty');empty.append(node('h2','Ничего не найдено'),node('p','Попробуйте другой запрос или выберите все категории.'));results.append(empty);}
   const pages=Math.ceil(filtered.length/6);if(pages>1)for(let i=1;i<=pages;i++){const b=node('button',String(i));if(page===i)b.setAttribute('aria-current','page');b.addEventListener('click',()=>{render(i);results.scrollIntoView({block:'start'});});pager.append(b);}
   links.forEach(a=>{if(a.dataset.category===category)a.setAttribute('aria-current','page');else a.removeAttribute('aria-current');});
 }
 async function load(){if(data)return true;try{const r=await fetch('/blog/search-index.json');if(!r.ok)throw Error();data=await r.json();return true;}catch{count.textContent='Поиск временно недоступен. Используйте категории ниже.';return false;}}
 input.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(async()=>{if(await load())render();},180);});
 links.forEach(a=>a.addEventListener('click',async e=>{if(!input.value.trim())return;e.preventDefault();if(await load()){category=a.dataset.category;render();}}));
 if(input.value)load().then(ok=>{if(ok)render();});
})();
