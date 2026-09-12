/* Real browser-print PDFs, shared as a file. No object URLs, popups or storage. */
(() => {
  'use strict';
  let active = null;
  const MAX_PDF_BYTES = 32 * 1024 * 1024;
  const escapeHtml = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  function buildDocument(title, style, content) {
    // Read-only clone: leave the on-screen report untouched. The server also
    // sanitizes and disables scripts/network access independently.
    const doc = new DOMParser().parseFromString('<!doctype html><html><head><meta charset="utf-8"><title>'+escapeHtml(title)+'</title><style>'+style+'</style></head><body>'+content+'</body></html>','text/html');
    doc.querySelectorAll('script,iframe,object,embed,link,base,meta[http-equiv],.no-print,button,input,select,textarea').forEach(node=>node.remove());
    doc.querySelectorAll('*').forEach(node=>{
      [...node.attributes].forEach(({name})=>{if (/^on/i.test(name)) node.removeAttribute(name);});
    });
    return '<!doctype html>'+doc.documentElement.outerHTML;
  }

  async function share(title, style, content) {
    if (active) {active.focus();return false;}
    const previousFocus=document.activeElement;
    const overlay=document.createElement('div');
    overlay.id='otomy-pdf-share';overlay.className='no-print';
    overlay.style.cssText='position:fixed;inset:0;z-index:2147483647;background:#0008;display:flex;align-items:center;justify-content:center;padding:20px';
    const panel=document.createElement('section');
    panel.setAttribute('role','dialog');panel.setAttribute('aria-modal','true');panel.setAttribute('aria-labelledby','otomy-pdf-title');panel.tabIndex=-1;
    panel.style.cssText='width:100%;max-width:390px;background:#fff;color:#14233b;border-radius:14px;padding:24px;font:16px/1.45 -apple-system,BlinkMacSystemFont,sans-serif;box-shadow:0 12px 40px #0005';
    const heading=document.createElement('h2');heading.id='otomy-pdf-title';heading.textContent='Preparing PDF';heading.style.cssText='margin:0 0 12px;font-size:21px';
    const status=document.createElement('p');status.setAttribute('role','status');status.textContent='Using the website print layout. Please keep the app open.';
    const shareButton=document.createElement('button');shareButton.type='button';shareButton.textContent='Share PDF';shareButton.hidden=true;
    const closeButton=document.createElement('button');closeButton.type='button';closeButton.textContent='Cancel';
    for(const button of [shareButton,closeButton]) button.style.cssText='padding:12px 18px;margin:8px 8px 0 0;border:1px solid #b5beca;border-radius:8px;font:600 16px -apple-system,sans-serif;background:#f3f5f9;color:#14233b';
    shareButton.style.background='#183d71';shareButton.style.color='#fff';
    panel.append(heading,status,shareButton,closeButton);overlay.append(panel);document.body.append(overlay);active=panel;panel.focus();
    let file=null,closed=false,sharing=false;
    const controller=new AbortController();
    const close=()=>{if(sharing)return;closed=true;controller.abort();file=null;overlay.remove();active=null;previousFocus?.focus();};
    closeButton.onclick=close;
    panel.onkeydown=event=>{
      if(event.key==='Escape'){event.preventDefault();close();}
      if(event.key==='Tab'){
        const buttons=[shareButton,closeButton].filter(b=>!b.hidden&&!b.disabled);
        if(!buttons.length){event.preventDefault();return;}
        if(event.shiftKey&&document.activeElement===buttons[0]){event.preventDefault();buttons.at(-1).focus();}
        else if(!event.shiftKey&&document.activeElement===buttons.at(-1)){event.preventDefault();buttons[0].focus();}
      }
    };
    const send=async()=>{
      if(!file||closed||sharing)return;
      sharing=true;shareButton.disabled=true;closeButton.disabled=true;
      try {
        await navigator.share({files:[file],title});
        sharing=false;close();
      } catch(error) {
        status.textContent=error?.name==='AbortError'?'Sharing cancelled. Your PDF is ready to share again.':'Your PDF is ready. Tap Share PDF to open the iPhone share sheet.';
      } finally {
        sharing=false;shareButton.disabled=false;closeButton.disabled=false;
      }
    };
    shareButton.onclick=send;
    const timeout=setTimeout(()=>controller.abort(),90000);
    try {
      if(typeof navigator.share!=='function'||typeof navigator.canShare!=='function')throw new Error('File sharing is unavailable in this browser. Use Safari or the installed Otomy app.');
      const html=buildDocument(title,style,content);
      if(new TextEncoder().encode(html).byteLength>4*1024*1024)throw new Error('This report is too large. Please choose a shorter date range.');
      const response=await fetch('/api/report/pdf',{method:'POST',credentials:'same-origin',cache:'no-store',redirect:'error',headers:{'Content-Type':'text/html; charset=utf-8'},body:html,signal:controller.signal});
      if(!response.ok){let message='PDF generation failed. Please retry.';try{message=(await response.json()).error||message;}catch{}throw new Error(message);}
      if(!response.headers.get('content-type')?.includes('application/pdf'))throw new Error('Please sign in to Otomy again, then retry.');
      const reader=response.body.getReader(),chunks=[];let length=0;
      while(true){const {done,value}=await reader.read();if(done)break;length+=value.byteLength;if(length>MAX_PDF_BYTES){await reader.cancel();throw new Error('PDF is too large to share. Choose a shorter date range.');}chunks.push(value);}
      const bytes=new Uint8Array(length);let offset=0;for(const chunk of chunks){bytes.set(chunk,offset);offset+=chunk.length;}
      if(length<100||new TextDecoder().decode(bytes.subarray(0,5))!=='%PDF-')throw new Error('The renderer did not return a valid PDF. Please retry.');
      if(closed)return false;
      const filename=(String(title||'Otomy Report').replace(/[^a-z0-9]+/gi,'-').replace(/^-|-$/g,'').slice(0,140)||'Otomy-Report')+'.pdf';
      file=new File([bytes],filename,{type:'application/pdf'});
      if(!navigator.canShare({files:[file]}))throw new Error('This device cannot share PDF files. Try the installed Otomy app or Safari.');
      heading.textContent='PDF ready';status.textContent='Tap Share PDF to send it to an app or save it to Files.';
      closeButton.textContent='Close';shareButton.hidden=false;shareButton.focus();
      // iOS consumes/expires activation during asynchronous work. Never open a
      // blank tab as a workaround; a fresh Share tap always remains available.
      if(navigator.userActivation?.isActive)await send();
      return true;
    }catch(error){
      if(!closed){heading.textContent='PDF not ready';status.textContent=error?.name==='AbortError'?'PDF preparation timed out. Close this message and try again.':error.message==='Failed to fetch'?'Could not reach the PDF renderer. Check your connection and Otomy sign-in, then retry.':error.message;closeButton.textContent='Close';}
      return false;
    }finally{clearTimeout(timeout);}
  }
  window.OtomyPdf={share,buildDocument};
})();
