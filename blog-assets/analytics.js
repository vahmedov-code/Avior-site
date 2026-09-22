(() => {
 const key='avior-blog-ref',article=document.querySelector('[data-article-id]');
 const goal=(name,id)=>{if(typeof window.ym==='function'&&id>0)window.ym(110953361,'reachGoal',name,{article_id:id});};
 const source=()=>{try{const v=JSON.parse(sessionStorage.getItem(key)||'null');return v&&Number.isInteger(v.id)&&Date.now()-v.at<1800000?v:null;}catch{return null;}};
 if(article){const id=Number(article.dataset.articleId);goal('blog_article_view',id);
   article.addEventListener('click',e=>{const a=e.target.closest('a');if(!a)return;if(a.hasAttribute('data-blog-service')||a.hasAttribute('data-blog-lead')){try{sessionStorage.setItem(key,JSON.stringify({id,at:Date.now()}));}catch{}goal(a.hasAttribute('data-blog-service')?'blog_service_click':'blog_lead_click',id);}});
 }
 // Existing handler sets this message only after a successful lead.php response.
 // Does not intercept requests, change payloads, or read form values.
 const note=document.querySelector('#leadForm .form-note');
 if(note)new MutationObserver(()=>{const ref=source();if(ref&&note.textContent.startsWith('Заявка отправлена')){goal('blog_lead_sent',ref.id);try{sessionStorage.removeItem(key);}catch{}}}).observe(note,{childList:true,subtree:true,characterData:true});
})();
