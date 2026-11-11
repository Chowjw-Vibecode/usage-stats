let language='zh',data=null,failed=false,product='all',level='cities';
const $=id=>document.getElementById(id);
const SUMMARY_URL='https://home.zhouchenxing.com/usage-stats/api/v2/summary';
const words={
  en:{brand:'Usage statistics',title:'Used around the world',intro:'Reporting installations by country, state / province and city.',product:'Product',all:'All products',this_cup:'OneCup',total:'Installations counted',active:'Active in 30 days',level:'Location level',levels:{countries:'Country / region',regions:'State / province',cities:'City'},identified:{countries:'Countries / regions identified',regions:'States / provinces identified',cities:'Cities identified'},count:'Total',recent:'Active · 30d',pending:'Statistics are not available yet.',categoryPending:'Statistics for this product are not available yet.',legacy:'This snapshot covers Decent Remote only. OneCup statistics are not available yet.',detailPending:'Some location details are pending. Older records need another device report; locations that cannot be identified stay in the pending group.',empty:'No installations have been counted for this selection yet.',failed:'Statistics could not be loaded. Please try again later.',updated:'Updated',note:'Locations come from the reporting IP’s exit location, not a person’s actual location. VPNs can change the result. These are installation counts, counted separately for each product; browser storage and reinstalls can also affect the figures.',unknown:'Unknown',unknownCountry:'Country / region unknown',regionPending:'State / province pending',cityPending:'City pending',detailsPending:'State / city pending'},
  zh:{brand:'使用统计',title:'各地都在用',intro:'按国家、省／州和城市汇总已上报的安装数量。',product:'分类',all:'全部',this_cup:'这一杯',total:'已统计安装',active:'近 30 天活跃',level:'统计层级',levels:{countries:'国家／地区',regions:'省／州',cities:'城市'},identified:{countries:'已识别国家／地区',regions:'已识别省／州',cities:'已识别城市'},count:'安装数',recent:'近 30 天活跃',pending:'统计暂未上线。',categoryPending:'该分类的统计暂未上线。',legacy:'当前快照仅包含 Decent Remote；这一杯分类暂未上线。',detailPending:'部分省市信息待更新：旧记录须等待设备再次上报，无法识别的位置仍列在待更新分组。',empty:'这个分类目前还没有安装统计。',failed:'暂时无法读取统计，请稍后再试。',updated:'更新于',note:'地区来自上报时的 IP 出口归属地，不代表人的实际位置；VPN 会影响归属。统计的是安装数，不同产品分别计数；浏览器存储和重新安装也可能影响数量。',unknown:'未知',unknownCountry:'国家／地区未知',regionPending:'省／州待更新',cityPending:'城市待更新',detailsPending:'省市待更新'}
};
const count=value=>Number.isSafeInteger(value)&&value>=0;
const validCounts=row=>row&&count(row.installations)&&count(row.active_30d)&&row.active_30d<=row.installations;
const country=value=>typeof value==='string'&&/^[A-Z]{2}$/.test(value);
const place=value=>value===null||typeof value==='string'&&value.trim().length>0;
const keyFor=(row,keys)=>JSON.stringify(keys.map(key=>row[key]));
function grouped(rows,keys){
  const out=new Map();
  for(const row of rows){
    if(!row.installations&&!row.active_30d)continue;
    const key=keyFor(row,keys),current=out.get(key)||{installations:0,active_30d:0};
    current.installations+=row.installations;current.active_30d+=row.active_30d;out.set(key,current);
  }
  return out;
}
function sameGroups(a,b,keys){
  const left=grouped(a,keys),right=grouped(b,keys);
  return left.size===right.size&&[...left].every(([key,value])=>right.get(key)?.installations===value.installations&&right.get(key)?.active_30d===value.active_30d);
}
function validRows(rows,keys,totals){
  return Array.isArray(rows)&&rows.every(row=>validCounts(row)&&country(row.country)&&keys.filter(key=>key!=='country').every(key=>place(row[key])))&&
    new Set(rows.map(row=>keyFor(row,keys))).size===rows.length&&
    rows.reduce((n,row)=>n+row.installations,0)===totals.total_installations&&rows.reduce((n,row)=>n+row.active_30d,0)===totals.active_30d;
}
function validTotals(value){
  if(!value||!count(value.total_installations)||!count(value.active_30d)||value.active_30d>value.total_installations||!validRows(value.countries,['country'],value))return false;
  for(const [field,keys]of [['regions',['country','region']],['cities',['country','region','city']]]){
    if(value[field]!==undefined&&(!validRows(value[field],keys,value)||!sameGroups(value[field],value.countries,['country'])))return false;
  }
  return !(value.regions&&value.cities)||sameGroups(value.cities,value.regions,['country','region']);
}
function selection(){
  if(product==='all')return data;
  if(data.schema===1)return product==='decent_remote'?data:null;
  return data.products.find(row=>row.product===product);
}
function rowsForLevel(selected){
  if(level==='countries')return selected.countries;
  if(level==='regions'){
    if(selected.regions)return selected.regions;
    if(selected.cities){
      return [...grouped(selected.cities,['country','region'])].map(([key,counts])=>{const [country,region]=JSON.parse(key);return {country,region,...counts}});
    }
    return selected.countries.map(row=>({...row,region:null}));
  }
  if(selected.cities)return selected.cities;
  return (selected.regions||selected.countries.map(row=>({...row,region:null}))).map(row=>({...row,city:null}));
}
function locationName(row,names,w){
  const name=row.country==='ZZ'?w.unknownCountry:(names?.of(row.country)||row.country);
  if(level==='countries')return name;
  if(level==='regions')return name+' → '+(row.region??w.regionPending);
  if(row.region===null&&row.city===null)return name+' → '+w.detailsPending;
  return name+' → '+(row.region??w.regionPending)+' → '+(row.city??w.cityPending);
}
function identified(row){return row.country!=='ZZ'&&(level==='countries'||level==='regions'&&row.region!==null||level==='cities'&&row.city!==null)}
function render(){
  const w=words[language];
  document.documentElement.lang=language==='en'?'en':'zh-CN';document.title=w.brand;
  $('brand').textContent=w.brand;$('language').textContent=language==='en'?'中文':'English';
  $('title').textContent=w.title;$('intro').textContent=w.intro;$('product-label').textContent=w.product;
  $('product').options[0].textContent=w.all;$('product').options[2].textContent=w.this_cup;
  $('level-label').textContent=w.level;
  for(const option of $('level').options)option.textContent=w.levels[option.value];
  for(const [id,key]of Object.entries({'total-label':'total','active-label':'active','count-label':'count','recent-label':'recent','note':'note'}))$(id).textContent=w[key];
  $('regions-label').textContent=w.identified[level];$('country-label').textContent=w.levels[level];
  $('rows').replaceChildren();$('version-rows').replaceChildren();$('version-section').hidden=true;$('table').hidden=true;$('updated').textContent='';
  for(const id of ['total','active','regions'])$(id).textContent='—';
  if(failed){$('message').textContent=w.failed;return}
  if(!data||data.status==='not_deployed'){$('message').textContent=w.pending;return}
  const selected=selection();
  if(!selected){$('message').textContent=w.categoryPending;return}
  const rows=rowsForLevel(selected).filter(row=>row.installations>0).slice().sort((a,b)=>b.installations-a.installations||keyFor(a,['country','region','city']).localeCompare(keyFor(b,['country','region','city'])));
  $('total').textContent=selected.total_installations.toLocaleString();$('active').textContent=selected.active_30d.toLocaleString();$('regions').textContent=rows.filter(identified).length;
  const detailsMissing=level!=='countries'&&rows.some(row=>level==='regions'?row.region===null:row.city===null||row.region===null);
  $('message').textContent=[data.schema===1?w.legacy:'',!rows.length?w.empty:detailsMissing?w.detailPending:''].filter(Boolean).join(' ');
  $('table').hidden=!rows.length;
  let names;try{names=new Intl.DisplayNames([language],{type:'region'})}catch{}
  const max=Math.max(1,...rows.map(row=>row.installations));
  for(const row of rows){
    const tr=document.createElement('tr'),name=document.createElement('td'),total=document.createElement('td'),active=document.createElement('td');
    name.textContent=locationName(row,names,w);
    const bar=document.createElement('div'),fill=document.createElement('i');bar.className='bar';fill.style.width=(100*row.installations/max)+'%';bar.append(fill);name.append(bar);
    total.textContent=row.installations.toLocaleString();active.textContent=row.active_30d.toLocaleString();tr.append(name,total,active);$('rows').append(tr);
  }
  $('version-section').hidden=false;$('version-title').textContent=language==='zh'?'版本分布':'Versions';
  $('version-note').textContent=language==='zh'?'按每个安装最近上报的版本计数。未提供版本号的旧客户端列为未知版本。':'Latest reported version per installation. Older clients without version information are shown as unknown.';
  $('version-product').textContent=language==='zh'?'产品':'Product';$('version-label').textContent=language==='zh'?'版本':'Version';$('version-count').textContent=w.count;$('version-active').textContent=w.recent;
  const groups=data.schema===2?data.products:[{product:'decent_remote',...data}];
  for(const group of groups.filter(group=>product==='all'||group.product===product)){
    const versions=Array.isArray(group.versions)?group.versions:[{version:null,installations:group.total_installations,active_30d:group.active_30d}];
    for(const row of versions){
      if(!row.installations)continue;
      const tr=document.createElement('tr');
      for(const value of [group.product==='this_cup'?w.this_cup:'Decent Remote',row.version??(language==='zh'?'未知版本':'Unknown version'),row.installations.toLocaleString(),row.active_30d.toLocaleString()]){const td=document.createElement('td');td.textContent=value;tr.append(td)}
      $('version-rows').append(tr);
    }
  }
  $('updated').textContent=w.updated+' '+new Date(data.generated_at).toLocaleString(language==='en'?'en-GB':'zh-CN');
}
$('language').onclick=()=>{language=language==='en'?'zh':'en';render()};
$('product').onchange=()=>{product=$('product').value;render()};
$('level').onchange=()=>{level=$('level').value;render()};
render();
function validateSummary(d){
  if(!d||typeof d!=='object')throw Error('invalid');
  if(d.status==='not_deployed')return d;
  if(![1,2].includes(d.schema)||!validTotals(d)||typeof d.generated_at!=='string'||!Number.isFinite(Date.parse(d.generated_at)))throw Error('invalid');
  if(d.schema===2){
    if(!Array.isArray(d.products)||d.products.length!==2||!['decent_remote','this_cup'].every(product=>d.products.filter(row=>row.product===product&&validTotals(row)).length===1))throw Error('invalid products');
    if(d.products.reduce((n,row)=>n+row.total_installations,0)!==d.total_installations||d.products.reduce((n,row)=>n+row.active_30d,0)!==d.active_30d)throw Error('invalid product totals');
    for(const [field,keys]of [['countries',['country']],['regions',['country','region']],['cities',['country','region','city']]])if(d[field]!==undefined&&d.products.every(row=>row[field]!==undefined)&&!sameGroups(d[field],d.products.flatMap(row=>row[field]),keys))throw Error('invalid product locations');
  }
  const versionGroups=d.schema===1?[d]:d.products;
  if(versionGroups.some(group=>group.versions!==undefined&&(!Array.isArray(group.versions)||group.versions.some(row=>!(row?.version===null||typeof row?.version==='string')||!validCounts(row))||group.versions.reduce((n,row)=>n+row.installations,0)!==group.total_installations||group.versions.reduce((n,row)=>n+row.active_30d,0)!==group.active_30d)))throw Error('invalid versions');
  return d;
}
async function readSummary(url){const response=await fetch(url,{credentials:'omit',cache:'no-store',signal:AbortSignal.timeout(8000)});if(!response.ok)throw Error('unavailable');return validateSummary(await response.json())}
readSummary(SUMMARY_URL).catch(()=>readSummary('./data.json')).then(value=>{data=value;render()}).catch(()=>{failed=true;render()});
