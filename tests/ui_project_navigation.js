// Playwright CLI run-code callback; no text changes, settings writes, import or audio generation.
async (page) => {
  const passed=[], errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const check=(name,ok)=>{if(!ok)throw Error(name);passed.push(name);};
  await page.setViewportSize({width:1440,height:960});
  await page.getByRole('button',{name:'声音',exact:true}).click();
  await page.evaluate(async()=>{await document.fonts.ready;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});
  await page.evaluate(()=>document.querySelector('#stage').scrollTop=220);
  const original=page.url(), text=await page.locator('.txt').evaluateAll(es=>es.map(e=>e.value)), position=await page.locator('#stage').evaluate(e=>e.scrollTop);
  await page.getByRole('button',{name:'设置',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('#draftRoot').value.length>0);
  check('settings is modal form',await page.locator('#dw-settings').evaluate(e=>e.open&&e.matches(':modal')));
  check('settings preserves project URL',page.url()===original);
  check('settings keeps editing context',await page.evaluate(()=>!document.body.classList.contains('scripts-home')&&document.body.dataset.processingStage==='audio'));
  await page.keyboard.press('Escape');
  check('escape closes settings',await page.locator('#dw-settings').evaluate(e=>!e.open));
  check('settings restores focus',await page.locator('#btnSettings').evaluate(e=>e===document.activeElement));
  check('settings preserves scroll',await page.locator('#stage').evaluate(e=>e.scrollTop)===position);
  await page.getByRole('button',{name:'帮助与反馈',exact:true}).click();
  await page.keyboard.press('Escape');
  check('help keeps project URL',page.url()===original);
  for(const name of ['项目','回收站','导入']){
    await page.getByRole('button',{name,exact:true}).click();
    await page.waitForFunction(()=>document.body.classList.contains('scripts-home'));
    check(name+' shows resume',await page.getByRole('button',{name:'返回当前项目',exact:true}).isVisible());
    await page.getByRole('button',{name:'返回当前项目',exact:true}).click();
    await page.waitForFunction(()=>!document.body.classList.contains('scripts-home'));
    check(name+' restores project and stage',page.url()===original);
    await page.waitForFunction(p=>document.querySelector('#stage').scrollTop===p,position);
    check(name+' restores reading position',true);
  }
  check('text unchanged',JSON.stringify(text)===JSON.stringify(await page.locator('.txt').evaluateAll(es=>es.map(e=>e.value))));
  await page.getByRole('button',{name:'项目',exact:true}).click();
  await page.getByRole('button',{name:'设置',exact:true}).click();
  await page.getByRole('button',{name:'关闭设置',exact:true}).click();
  check('settings from library stays in library',await page.evaluate(()=>document.body.classList.contains('scripts-home')));
  await page.getByRole('button',{name:'返回当前项目',exact:true}).click();
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'设置',exact:true}).click();
  check('mobile settings fits viewport',await page.locator('#dw-settings').evaluate(e=>{const r=e.getBoundingClientRect();return r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight;}));
  await page.getByRole('button',{name:'关闭设置',exact:true}).click();
  await page.setViewportSize({width:1440,height:960});
  await page.getByRole('button',{name:'文案',exact:true}).click();
  check('no runtime errors',errors.length===0);
  return {passed,errors};
}
