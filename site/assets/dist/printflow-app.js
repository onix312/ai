var lt=Object.defineProperty;var ct=(i,t,e)=>t in i?lt(i,t,{enumerable:!0,configurable:!0,writable:!0,value:e}):i[t]=e;var N=(i,t,e)=>ct(i,typeof t!="symbol"?t+"":t,e);/**
 * @license
 * Copyright 2019 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const T=globalThis,F=T.ShadowRoot&&(T.ShadyCSS===void 0||T.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,j=Symbol(),B=new WeakMap;let st=class{constructor(t,e,s){if(this._$cssResult$=!0,s!==j)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(F&&t===void 0){const s=e!==void 0&&e.length===1;s&&(t=B.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),s&&B.set(e,t))}return t}toString(){return this.cssText}};const ht=i=>new st(typeof i=="string"?i:i+"",void 0,j),dt=(i,...t)=>{const e=i.length===1?i[0]:t.reduce((s,r,o)=>s+(n=>{if(n._$cssResult$===!0)return n.cssText;if(typeof n=="number")return n;throw Error("Value passed to 'css' function must be a 'css' function result: "+n+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(r)+i[o+1],i[0]);return new st(e,i,j)},pt=(i,t)=>{if(F)i.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(const e of t){const s=document.createElement("style"),r=T.litNonce;r!==void 0&&s.setAttribute("nonce",r),s.textContent=e.cssText,i.appendChild(s)}},V=F?i=>i:i=>i instanceof CSSStyleSheet?(t=>{let e="";for(const s of t.cssRules)e+=s.cssText;return ht(e)})(i):i;/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const{is:ut,defineProperty:ft,getOwnPropertyDescriptor:vt,getOwnPropertyNames:bt,getOwnPropertySymbols:gt,getPrototypeOf:$t}=Object,g=globalThis,W=g.trustedTypes,mt=W?W.emptyScript:"",_t=g.reactiveElementPolyfillSupport,E=(i,t)=>i,D={toAttribute(i,t){switch(t){case Boolean:i=i?mt:null;break;case Object:case Array:i=i==null?i:JSON.stringify(i)}return i},fromAttribute(i,t){let e=i;switch(t){case Boolean:e=i!==null;break;case Number:e=i===null?null:Number(i);break;case Object:case Array:try{e=JSON.parse(i)}catch{e=null}}return e}},rt=(i,t)=>!ut(i,t),Z={attribute:!0,type:String,converter:D,reflect:!1,useDefault:!1,hasChanged:rt};Symbol.metadata??(Symbol.metadata=Symbol("metadata")),g.litPropertyMetadata??(g.litPropertyMetadata=new WeakMap);let y=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??(this.l=[])).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=Z){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const s=Symbol(),r=this.getPropertyDescriptor(t,s,e);r!==void 0&&ft(this.prototype,t,r)}}static getPropertyDescriptor(t,e,s){const{get:r,set:o}=vt(this.prototype,t)??{get(){return this[e]},set(n){this[e]=n}};return{get:r,set(n){const c=r?.call(this);o?.call(this,n),this.requestUpdate(t,c,s)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??Z}static _$Ei(){if(this.hasOwnProperty(E("elementProperties")))return;const t=$t(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(E("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(E("properties"))){const e=this.properties,s=[...bt(e),...gt(e)];for(const r of s)this.createProperty(r,e[r])}const t=this[Symbol.metadata];if(t!==null){const e=litPropertyMetadata.get(t);if(e!==void 0)for(const[s,r]of e)this.elementProperties.set(s,r)}this._$Eh=new Map;for(const[e,s]of this.elementProperties){const r=this._$Eu(e,s);r!==void 0&&this._$Eh.set(r,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const s=new Set(t.flat(1/0).reverse());for(const r of s)e.unshift(V(r))}else t!==void 0&&e.push(V(t));return e}static _$Eu(t,e){const s=e.attribute;return s===!1?void 0:typeof s=="string"?s:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??(this._$EO=new Set)).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const s of e.keys())this.hasOwnProperty(s)&&(t.set(s,this[s]),delete this[s]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return pt(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??(this.renderRoot=this.createRenderRoot()),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,s){this._$AK(t,s)}_$ET(t,e){const s=this.constructor.elementProperties.get(t),r=this.constructor._$Eu(t,s);if(r!==void 0&&s.reflect===!0){const o=(s.converter?.toAttribute!==void 0?s.converter:D).toAttribute(e,s.type);this._$Em=t,o==null?this.removeAttribute(r):this.setAttribute(r,o),this._$Em=null}}_$AK(t,e){const s=this.constructor,r=s._$Eh.get(t);if(r!==void 0&&this._$Em!==r){const o=s.getPropertyOptions(r),n=typeof o.converter=="function"?{fromAttribute:o.converter}:o.converter?.fromAttribute!==void 0?o.converter:D;this._$Em=r;const c=n.fromAttribute(e,o.type);this[r]=c??this._$Ej?.get(r)??c,this._$Em=null}}requestUpdate(t,e,s,r=!1,o){if(t!==void 0){const n=this.constructor;if(r===!1&&(o=this[t]),s??(s=n.getPropertyOptions(t)),!((s.hasChanged??rt)(o,e)||s.useDefault&&s.reflect&&o===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,s))))return;this.C(t,e,s)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:s,reflect:r,wrapped:o},n){s&&!(this._$Ej??(this._$Ej=new Map)).has(t)&&(this._$Ej.set(t,n??e??this[t]),o!==!0||n!==void 0)||(this._$AL.has(t)||(this.hasUpdated||s||(e=void 0),this._$AL.set(t,e)),r===!0&&this._$Em!==t&&(this._$Eq??(this._$Eq=new Set)).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}const t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??(this.renderRoot=this.createRenderRoot()),this._$Ep){for(const[r,o]of this._$Ep)this[r]=o;this._$Ep=void 0}const s=this.constructor.elementProperties;if(s.size>0)for(const[r,o]of s){const{wrapped:n}=o,c=this[r];n!==!0||this._$AL.has(r)||c===void 0||this.C(r,void 0,o,c)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(s=>s.hostUpdate?.()),this.update(e)):this._$EM()}catch(s){throw t=!1,this._$EM(),s}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&(this._$Eq=this._$Eq.forEach(e=>this._$ET(e,this[e]))),this._$EM()}updated(t){}firstUpdated(t){}};y.elementStyles=[],y.shadowRootOptions={mode:"open"},y[E("elementProperties")]=new Map,y[E("finalized")]=new Map,_t?.({ReactiveElement:y}),(g.reactiveElementVersions??(g.reactiveElementVersions=[])).push("2.1.2");/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const S=globalThis,J=i=>i,H=S.trustedTypes,K=H?H.createPolicy("lit-html",{createHTML:i=>i}):void 0,it="$lit$",b=`lit$${Math.random().toFixed(9).slice(2)}$`,ot="?"+b,yt=`<${ot}>`,_=document,P=()=>_.createComment(""),U=i=>i===null||typeof i!="object"&&typeof i!="function",Q=Array.isArray,At=i=>Q(i)||typeof i?.[Symbol.iterator]=="function",z=`[ 	
\f\r]`,x=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,Y=/-->/g,G=/>/g,$=RegExp(`>|${z}(?:([^\\s"'>=/]+)(${z}*=${z}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`,"g"),X=/'/g,tt=/"/g,nt=/^(?:script|style|textarea|title)$/i,wt=i=>(t,...e)=>({_$litType$:i,strings:t,values:e}),f=wt(1),A=Symbol.for("lit-noChange"),d=Symbol.for("lit-nothing"),et=new WeakMap,m=_.createTreeWalker(_,129);function at(i,t){if(!Q(i)||!i.hasOwnProperty("raw"))throw Error("invalid template strings array");return K!==void 0?K.createHTML(t):t}const xt=(i,t)=>{const e=i.length-1,s=[];let r,o=t===2?"<svg>":t===3?"<math>":"",n=x;for(let c=0;c<e;c++){const a=i[c];let h,p,l=-1,u=0;for(;u<a.length&&(n.lastIndex=u,p=n.exec(a),p!==null);)u=n.lastIndex,n===x?p[1]==="!--"?n=Y:p[1]!==void 0?n=G:p[2]!==void 0?(nt.test(p[2])&&(r=RegExp("</"+p[2],"g")),n=$):p[3]!==void 0&&(n=$):n===$?p[0]===">"?(n=r??x,l=-1):p[1]===void 0?l=-2:(l=n.lastIndex-p[2].length,h=p[1],n=p[3]===void 0?$:p[3]==='"'?tt:X):n===tt||n===X?n=$:n===Y||n===G?n=x:(n=$,r=void 0);const v=n===$&&i[c+1].startsWith("/>")?" ":"";o+=n===x?a+yt:l>=0?(s.push(h),a.slice(0,l)+it+a.slice(l)+b+v):a+b+(l===-2?c:v)}return[at(i,o+(i[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),s]};class R{constructor({strings:t,_$litType$:e},s){let r;this.parts=[];let o=0,n=0;const c=t.length-1,a=this.parts,[h,p]=xt(t,e);if(this.el=R.createElement(h,s),m.currentNode=this.el.content,e===2||e===3){const l=this.el.content.firstChild;l.replaceWith(...l.childNodes)}for(;(r=m.nextNode())!==null&&a.length<c;){if(r.nodeType===1){if(r.hasAttributes())for(const l of r.getAttributeNames())if(l.endsWith(it)){const u=p[n++],v=r.getAttribute(l).split(b),O=/([.?@])?(.*)/.exec(u);a.push({type:1,index:o,name:O[2],strings:v,ctor:O[1]==="."?St:O[1]==="?"?kt:O[1]==="@"?Ct:M}),r.removeAttribute(l)}else l.startsWith(b)&&(a.push({type:6,index:o}),r.removeAttribute(l));if(nt.test(r.tagName)){const l=r.textContent.split(b),u=l.length-1;if(u>0){r.textContent=H?H.emptyScript:"";for(let v=0;v<u;v++)r.append(l[v],P()),m.nextNode(),a.push({type:2,index:++o});r.append(l[u],P())}}}else if(r.nodeType===8)if(r.data===ot)a.push({type:2,index:o});else{let l=-1;for(;(l=r.data.indexOf(b,l+1))!==-1;)a.push({type:7,index:o}),l+=b.length-1}o++}}static createElement(t,e){const s=_.createElement("template");return s.innerHTML=t,s}}function w(i,t,e=i,s){if(t===A)return t;let r=s!==void 0?e._$Co?.[s]:e._$Cl;const o=U(t)?void 0:t._$litDirective$;return r?.constructor!==o&&(r?._$AO?.(!1),o===void 0?r=void 0:(r=new o(i),r._$AT(i,e,s)),s!==void 0?(e._$Co??(e._$Co=[]))[s]=r:e._$Cl=r),r!==void 0&&(t=w(i,r._$AS(i,t.values),r,s)),t}class Et{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:s}=this._$AD,r=(t?.creationScope??_).importNode(e,!0);m.currentNode=r;let o=m.nextNode(),n=0,c=0,a=s[0];for(;a!==void 0;){if(n===a.index){let h;a.type===2?h=new q(o,o.nextSibling,this,t):a.type===1?h=new a.ctor(o,a.name,a.strings,this,t):a.type===6&&(h=new Pt(o,this,t)),this._$AV.push(h),a=s[++c]}n!==a?.index&&(o=m.nextNode(),n++)}return m.currentNode=_,r}p(t){let e=0;for(const s of this._$AV)s!==void 0&&(s.strings!==void 0?(s._$AI(t,s,e),e+=s.strings.length-2):s._$AI(t[e])),e++}}class q{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,s,r){this.type=2,this._$AH=d,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=s,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=w(this,t,e),U(t)?t===d||t==null||t===""?(this._$AH!==d&&this._$AR(),this._$AH=d):t!==this._$AH&&t!==A&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):At(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==d&&U(this._$AH)?this._$AA.nextSibling.data=t:this.T(_.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:s}=t,r=typeof s=="number"?this._$AC(t):(s.el===void 0&&(s.el=R.createElement(at(s.h,s.h[0]),this.options)),s);if(this._$AH?._$AD===r)this._$AH.p(e);else{const o=new Et(r,this),n=o.u(this.options);o.p(e),this.T(n),this._$AH=o}}_$AC(t){let e=et.get(t.strings);return e===void 0&&et.set(t.strings,e=new R(t)),e}k(t){Q(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let s,r=0;for(const o of t)r===e.length?e.push(s=new q(this.O(P()),this.O(P()),this,this.options)):s=e[r],s._$AI(o),r++;r<e.length&&(this._$AR(s&&s._$AB.nextSibling,r),e.length=r)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const s=J(t).nextSibling;J(t).remove(),t=s}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}}class M{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,s,r,o){this.type=1,this._$AH=d,this._$AN=void 0,this.element=t,this.name=e,this._$AM=r,this.options=o,s.length>2||s[0]!==""||s[1]!==""?(this._$AH=Array(s.length-1).fill(new String),this.strings=s):this._$AH=d}_$AI(t,e=this,s,r){const o=this.strings;let n=!1;if(o===void 0)t=w(this,t,e,0),n=!U(t)||t!==this._$AH&&t!==A,n&&(this._$AH=t);else{const c=t;let a,h;for(t=o[0],a=0;a<o.length-1;a++)h=w(this,c[s+a],e,a),h===A&&(h=this._$AH[a]),n||(n=!U(h)||h!==this._$AH[a]),h===d?t=d:t!==d&&(t+=(h??"")+o[a+1]),this._$AH[a]=h}n&&!r&&this.j(t)}j(t){t===d?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class St extends M{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===d?void 0:t}}class kt extends M{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==d)}}class Ct extends M{constructor(t,e,s,r,o){super(t,e,s,r,o),this.type=5}_$AI(t,e=this){if((t=w(this,t,e,0)??d)===A)return;const s=this._$AH,r=t===d&&s!==d||t.capture!==s.capture||t.once!==s.once||t.passive!==s.passive,o=t!==d&&(s===d||r);r&&this.element.removeEventListener(this.name,this,s),o&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class Pt{constructor(t,e,s){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=s}get _$AU(){return this._$AM._$AU}_$AI(t){w(this,t)}}const Ut=S.litHtmlPolyfillSupport;Ut?.(R,q),(S.litHtmlVersions??(S.litHtmlVersions=[])).push("3.3.3");const Rt=(i,t,e)=>{const s=e?.renderBefore??t;let r=s._$litPart$;if(r===void 0){const o=e?.renderBefore??null;s._$litPart$=r=new q(t.insertBefore(P(),o),o,void 0,e??{})}return r._$AI(i),r};/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const k=globalThis;class C extends y{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){var e;const t=super.createRenderRoot();return(e=this.renderOptions).renderBefore??(e.renderBefore=t.firstChild),t}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=Rt(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return A}}C._$litElement$=!0,C.finalized=!0,k.litElementHydrateSupport?.({LitElement:C});const qt=k.litElementPolyfillSupport;qt?.({LitElement:C});(k.litElementVersions??(k.litElementVersions=[])).push("4.2.2");const Ot=[{href:"/",file:"index.html",icon:"dashboard",cat:"admin",title:"Панель управления",sub:"15 разделов: цех, склад, касса, настройки"},{href:"/cashier",file:"cashier.html",icon:"ruble",cat:"tool",lock:!0,title:"Мобильная касса",sub:"Продажа с телефона, код доступа"},{href:"/sbp",file:"sbp.html",icon:"qr",cat:"tool",lock:!0,title:"СБП · входящие",sub:"Платежи по QR, авто-подтверждение"},{href:"/bank",file:"bank.html",icon:"bank",cat:"tool",lock:!0,title:"Поступления из банка",sub:"Выписка и сопоставление с заказами"},{href:"/m",file:"m.html",icon:"printer",cat:"tool",title:"Станок (мобильная)",sub:"Пульты у принтера: старт, пауза, съём"},{href:"/shelf",file:"shelf.html",icon:"shelf",cat:"tv",title:"Экран стеллажа",sub:"Витрина полки для покупателей"},{href:"/tv",file:"tv.html",icon:"tv",cat:"tv",title:"ТВ-дашборд",sub:"Очередь и статусы на большой экран"},{href:"/order",file:"order.html",icon:"cart",cat:"shop",title:"Витрина и заказ",sub:"Каталог, корзина, заявка с телефона"},{href:"/track",file:"track.html",icon:"track",cat:"shop",title:"Статус заказа",sub:"Покупатель следит за готовностью"},{href:"/my",file:"my.html",icon:"user",cat:"shop",title:"Мои заказы",sub:"Личный кабинет покупателя"},{href:"/spool",file:"spool.html",icon:"spool",cat:"tool",title:"Катушки",sub:"Взвешивание и списание пластика"},{href:"/labels",file:"labels.html",icon:"tag",cat:"print",title:"Этикетки",sub:"Печать наклеек и бирок с QR"},{href:"/price-tags",file:"price-tags.html",icon:"tag",cat:"print",title:"Ценники",sub:"Ценники и промостенды 67×32"},{href:"/design",file:"design.html",icon:"pen",cat:"shop",title:"Заявка на дизайн",sub:"Приём индивидуальных заказов"}],I={admin:{label:"Панель",tone:"accent"},tool:{label:"Сотрудникам",tone:"info"},tv:{label:"Экраны",tone:"violet"},shop:{label:"Покупателям",tone:"ok"},print:{label:"Печать",tone:"warn"}};class L extends C{constructor(){super(),this.filter="",this.cat="all",this.qrFor=null}get pages(){const t=this.filter.trim().toLowerCase();return Ot.filter(e=>this.cat!=="all"&&e.cat!==this.cat?!1:t?(e.title+" "+e.sub+" "+e.file).toLowerCase().includes(t):!0)}absUrl(t){return location.origin+t}firstUpdated(){let t=0;const e=setInterval(()=>{t++;const s=!!(window.QR&&window.QR.svg),r=!!(window.PFIcons&&window.PFIcons.svg);(s&&r||t>20)&&(clearInterval(e),this.requestUpdate())},250)}qrSvg(t){try{const e=window.QR;return!e||typeof e.svg!="function"?"":e.svg(this.absUrl(t),{ecl:"M",size:240,margin:1,dark:"#0f172a",light:"#ffffff"})}catch{return""}}iconSvg(t){try{if(window.PFIcons&&typeof window.PFIcons.svg=="function"){const s=window.PFIcons.svg(t);if(s)return s}}catch{}return`<span style="font-size:18px">${{dashboard:"◫",ruble:"₽",qr:"▦",bank:"🏦",printer:"🖨",shelf:"📦",tv:"📺",cart:"🛒",track:"📍",user:"👤",spool:"🧵",tag:"🏷",pen:"✏"}[t]||"◈"}</span>`}qrAvailable(){return!!(window.QR&&typeof window.QR.svg=="function")}render(){const t=this.pages;return f`
      <div class="hub-toolbar">
        <label class="hub-search">
          <span aria-hidden="true">⌕</span>
          <input type="search" placeholder="Найти страницу: касса, трек, полка…"
                 .value=${this.filter}
                 @input=${e=>this.filter=e.target.value}
                 aria-label="Поиск по страницам">
        </label>
        <div class="hub-cats" role="group" aria-label="Фильтр страниц">
          <button type="button" class="hub-cat ${this.cat==="all"?"on":""}"
                  @click=${()=>this.cat="all"}>Все</button>
          ${Object.entries(I).map(([e,s])=>f`
            <button type="button" class="hub-cat ${this.cat===e?"on":""}"
                    @click=${()=>this.cat=e}>${s.label}</button>`)}
        </div>
      </div>

      ${t.length===0?f`<div class="hub-empty">Ничего не нашлось — попробуйте другой запрос.</div>`:f`<div class="hub-grid">
            ${t.map(e=>f`
              <a class="pcard" href=${e.href} data-view="">
                <div class="pcard-top">
                  <span class="pcard-ic" .innerHTML=${this.iconSvg(e.icon)||"◈"}></span>
                  <span class="pcard-title">${e.title}</span>
                  <button type="button" class="pcard-qr" title="QR для открытия с телефона"
                          aria-label="QR-код страницы «${e.title}»"
                          @click=${s=>{s.preventDefault(),s.stopPropagation(),this.qrFor=e}}>▦</button>
                </div>
                <span class="pcard-sub">${e.sub}</span>
                <div class="pcard-meta">
                  <span class="badge tone-${I[e.cat].tone}">${I[e.cat].label}</span>
                  ${e.lock?f`<span class="badge lock" title="Требуется код доступа">🔒 код</span>`:""}
                </div>
                <span class="pcard-addr">${e.file}</span>
              </a>`)}
          </div>`}

      ${this.qrFor?f`
        <div class="qr-overlay" role="dialog" aria-modal="true"
             aria-label="QR страницы ${this.qrFor.title}"
             @click=${e=>{e.target===e.currentTarget&&(this.qrFor=null)}}>
          <div class="qr-modal">
            <h3>${this.qrFor.title}</h3>
            <p>Наведите камеру телефона — страница откроется в локальной сети</p>
            ${this.qrAvailable()?f`<div class="qr-box" .innerHTML=${this.qrSvg(this.qrFor.href)}></div>`:f`<div class="qr-box" style="display:grid;place-items:center;color:var(--muted);font-size:13px;line-height:1.4">QR-генератор загружается…<br>обновление страницы помогает</div>`}
            <div class="qr-addr">${this.absUrl(this.qrFor.href)}</div>
            <div class="qr-actions">
              <button type="button" class="btn" @click=${()=>this.qrFor=null}>Закрыть</button>
              <a class="btn primary" href=${this.qrFor.href}>Открыть</a>
            </div>
          </div>
        </div>`:""}
    `}}N(L,"properties",{filter:{state:!0},cat:{state:!0},qrFor:{state:!0}}),N(L,"styles",dt`
    :host { display: block; }
    .hub-toolbar {
      display: flex; flex-wrap: wrap; gap: var(--sp-3); align-items: center;
      margin: var(--sp-4) 0;
    }
    .hub-search {
      flex: 1 1 220px; display: flex; align-items: center; gap: var(--sp-2);
      background: var(--input-bg); border: 1px solid var(--input-line);
      border-radius: var(--input-radius); padding: 0 var(--sp-3);
      height: var(--input-h);
    }
    .hub-search:focus-within {
      background: var(--input-bg-focus); border-color: var(--input-line-focus);
      box-shadow: var(--focus-ring);
    }
    .hub-search input {
      flex: 1; border: 0; background: transparent; color: var(--input-text);
      font: inherit; outline: none; font-size: var(--fs-base);
    }
    .hub-cats { display: flex; flex-wrap: wrap; gap: var(--sp-2); }
    .hub-cat {
      border: 1px solid var(--btn-line); background: var(--btn-bg);
      color: var(--btn-text); border-radius: var(--chip-radius);
      padding: 6px 12px; font-size: var(--fs-sm); font-weight: var(--fw-medium);
      cursor: pointer; line-height: 1;
    }
    .hub-cat:hover { background: var(--btn-bg-hover); }
    .hub-cat.on {
      background: var(--accent); border-color: var(--accent); color: var(--accent-ink);
    }
    .hub-grid {
      display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr));
      gap: var(--sp-4);
    }
    .pcard {
      position: relative; display: flex; flex-direction: column; gap: var(--sp-2);
      background: var(--card-bg); border: 1px solid var(--card-line);
      border-radius: var(--card-radius); padding: var(--sp-4);
      text-decoration: none; color: var(--text);
      box-shadow: var(--elev-1);
    }
    .pcard:hover {
      transform: translateY(var(--card-hover-y)); box-shadow: var(--elev-2);
      border-color: var(--accent-line);
    }
    .pcard-top { display: flex; align-items: center; gap: var(--sp-3); }
    .pcard-ic {
      width: 40px; height: 40px; flex: none; border-radius: var(--r-sm);
      display: grid; place-items: center;
      background: var(--accent-soft); color: var(--accent);
    }
    .pcard-ic ::slotted(svg), .pcard-ic svg { width: 22px; height: 22px; }
    .pcard-title { font-weight: var(--fw-bold); font-size: var(--fs-md); line-height: 1.25; }
    .pcard-sub { color: var(--muted); font-size: var(--fs-sm); line-height: var(--lh-base); }
    .pcard-meta { display: flex; align-items: center; gap: var(--sp-2); margin-top: var(--sp-1); }
    .badge {
      font-size: var(--fs-xs); font-weight: var(--fw-medium);
      padding: 3px 9px; border-radius: var(--chip-radius);
      background: var(--panel-3); color: var(--text-2);
    }
    .badge.tone-accent { background: var(--accent-soft); color: var(--accent); }
    .badge.tone-info { background: var(--info-soft); color: var(--info); }
    .badge.tone-ok { background: var(--ok-soft); color: var(--ok); }
    .badge.tone-warn { background: var(--warn-soft); color: var(--warn); }
    .badge.tone-violet { background: rgba(124,58,237,.14); color: var(--accent-2); }
    .badge.lock { background: var(--warn-soft); color: var(--warn); }
    .pcard-qr {
      margin-left: auto; border: 1px solid var(--btn-line); background: var(--btn-bg);
      color: var(--btn-text); border-radius: var(--r-xs);
      width: 30px; height: 30px; display: grid; place-items: center;
      cursor: pointer; font-size: 15px;
    }
    .pcard-qr:hover { background: var(--btn-bg-hover); }
    .pcard-addr {
      font-family: var(--mono); font-size: var(--fs-xs); color: var(--muted);
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .hub-empty {
      padding: var(--sp-8) var(--sp-4); text-align: center; color: var(--muted);
      border: 1px dashed var(--line-strong); border-radius: var(--card-radius);
    }
    /* модалка QR */
    .qr-overlay {
      position: fixed; inset: 0; background: var(--modal-overlay);
      display: grid; place-items: center; z-index: 80; padding: var(--sp-4);
    }
    .qr-modal {
      background: var(--modal-bg); border-radius: var(--modal-radius);
      box-shadow: var(--modal-shadow); padding: var(--sp-6);
      max-width: 360px; width: 100%; text-align: center;
    }
    .qr-modal h3 { margin: 0 0 var(--sp-1); font-size: var(--fs-lg); }
    .qr-modal p { margin: 0 0 var(--sp-4); color: var(--muted); font-size: var(--fs-sm); }
    .qr-box {
      background: #fff; border-radius: var(--r); padding: var(--sp-3);
      width: 240px; height: 240px; margin: 0 auto var(--sp-4);
      display: grid; place-items: center;
    }
    .qr-box svg { width: 100%; height: 100%; }
    .qr-addr {
      font-family: var(--mono); font-size: var(--fs-sm); word-break: break-all;
      color: var(--text-2); margin-bottom: var(--sp-4);
    }
    .qr-actions { display: flex; gap: var(--sp-3); justify-content: center; }
    .btn {
      display: inline-flex; align-items: center; justify-content: center; gap: var(--sp-2);
      border: 1px solid var(--btn-line); background: var(--btn-bg); color: var(--btn-text);
      border-radius: var(--btn-radius); padding: 8px 16px; font-size: var(--fs-sm);
      font-weight: var(--fw-medium); cursor: pointer; text-decoration: none; line-height: 1.2;
      transition: background var(--t-fast), border-color var(--t-fast);
    }
    .btn:hover { background: var(--btn-bg-hover); }
    .btn.primary {
      background: var(--accent); border-color: var(--accent); color: var(--accent-ink);
    }
    .btn.primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
    @media (prefers-reduced-motion: reduce) {
      .pcard { transition: none; }
      .pcard:hover { transform: none; }
    }
  `);customElements.define("pf-pages-hub",L);window.PF_DIST_READY=!0;
