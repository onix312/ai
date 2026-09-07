var lt=Object.defineProperty;var ct=(i,t,e)=>t in i?lt(i,t,{enumerable:!0,configurable:!0,writable:!0,value:e}):i[t]=e;var N=(i,t,e)=>ct(i,typeof t!="symbol"?t+"":t,e);/**
 * @license
 * Copyright 2019 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const H=globalThis,I=H.ShadowRoot&&(H.ShadyCSS===void 0||H.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,F=Symbol(),Q=new WeakMap;let rt=class{constructor(t,e,r){if(this._$cssResult$=!0,r!==F)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(I&&t===void 0){const r=e!==void 0&&e.length===1;r&&(t=Q.get(e)),t===void 0&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),r&&Q.set(e,t))}return t}toString(){return this.cssText}};const ht=i=>new rt(typeof i=="string"?i:i+"",void 0,F),dt=(i,...t)=>{const e=i.length===1?i[0]:t.reduce((r,s,o)=>r+(n=>{if(n._$cssResult$===!0)return n.cssText;if(typeof n=="number")return n;throw Error("Value passed to 'css' function must be a 'css' function result: "+n+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(s)+i[o+1],i[0]);return new rt(e,i,F)},pt=(i,t)=>{if(I)i.adoptedStyleSheets=t.map(e=>e instanceof CSSStyleSheet?e:e.styleSheet);else for(const e of t){const r=document.createElement("style"),s=H.litNonce;s!==void 0&&r.setAttribute("nonce",s),r.textContent=e.cssText,i.appendChild(r)}},V=I?i=>i:i=>i instanceof CSSStyleSheet?(t=>{let e="";for(const r of t.cssRules)e+=r.cssText;return ht(e)})(i):i;/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const{is:ut,defineProperty:ft,getOwnPropertyDescriptor:vt,getOwnPropertyNames:gt,getOwnPropertySymbols:$t,getPrototypeOf:bt}=Object,g=globalThis,W=g.trustedTypes,mt=W?W.emptyScript:"",_t=g.reactiveElementPolyfillSupport,E=(i,t)=>i,L={toAttribute(i,t){switch(t){case Boolean:i=i?mt:null;break;case Object:case Array:i=i==null?i:JSON.stringify(i)}return i},fromAttribute(i,t){let e=i;switch(t){case Boolean:e=i!==null;break;case Number:e=i===null?null:Number(i);break;case Object:case Array:try{e=JSON.parse(i)}catch{e=null}}return e}},st=(i,t)=>!ut(i,t),Z={attribute:!0,type:String,converter:L,reflect:!1,useDefault:!1,hasChanged:st};Symbol.metadata??(Symbol.metadata=Symbol("metadata")),g.litPropertyMetadata??(g.litPropertyMetadata=new WeakMap);let y=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??(this.l=[])).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=Z){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const r=Symbol(),s=this.getPropertyDescriptor(t,r,e);s!==void 0&&ft(this.prototype,t,s)}}static getPropertyDescriptor(t,e,r){const{get:s,set:o}=vt(this.prototype,t)??{get(){return this[e]},set(n){this[e]=n}};return{get:s,set(n){const c=s?.call(this);o?.call(this,n),this.requestUpdate(t,c,r)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??Z}static _$Ei(){if(this.hasOwnProperty(E("elementProperties")))return;const t=bt(this);t.finalize(),t.l!==void 0&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty(E("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty(E("properties"))){const e=this.properties,r=[...gt(e),...$t(e)];for(const s of r)this.createProperty(s,e[s])}const t=this[Symbol.metadata];if(t!==null){const e=litPropertyMetadata.get(t);if(e!==void 0)for(const[r,s]of e)this.elementProperties.set(r,s)}this._$Eh=new Map;for(const[e,r]of this.elementProperties){const s=this._$Eu(e,r);s!==void 0&&this._$Eh.set(s,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const r=new Set(t.flat(1/0).reverse());for(const s of r)e.unshift(V(s))}else t!==void 0&&e.push(V(t));return e}static _$Eu(t,e){const r=e.attribute;return r===!1?void 0:typeof r=="string"?r:typeof t=="string"?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??(this._$EO=new Set)).add(t),this.renderRoot!==void 0&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const r of e.keys())this.hasOwnProperty(r)&&(t.set(r,this[r]),delete this[r]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return pt(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??(this.renderRoot=this.createRenderRoot()),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,r){this._$AK(t,r)}_$ET(t,e){const r=this.constructor.elementProperties.get(t),s=this.constructor._$Eu(t,r);if(s!==void 0&&r.reflect===!0){const o=(r.converter?.toAttribute!==void 0?r.converter:L).toAttribute(e,r.type);this._$Em=t,o==null?this.removeAttribute(s):this.setAttribute(s,o),this._$Em=null}}_$AK(t,e){const r=this.constructor,s=r._$Eh.get(t);if(s!==void 0&&this._$Em!==s){const o=r.getPropertyOptions(s),n=typeof o.converter=="function"?{fromAttribute:o.converter}:o.converter?.fromAttribute!==void 0?o.converter:L;this._$Em=s;const c=n.fromAttribute(e,o.type);this[s]=c??this._$Ej?.get(s)??c,this._$Em=null}}requestUpdate(t,e,r,s=!1,o){if(t!==void 0){const n=this.constructor;if(s===!1&&(o=this[t]),r??(r=n.getPropertyOptions(t)),!((r.hasChanged??st)(o,e)||r.useDefault&&r.reflect&&o===this._$Ej?.get(t)&&!this.hasAttribute(n._$Eu(t,r))))return;this.C(t,e,r)}this.isUpdatePending===!1&&(this._$ES=this._$EP())}C(t,e,{useDefault:r,reflect:s,wrapped:o},n){r&&!(this._$Ej??(this._$Ej=new Map)).has(t)&&(this._$Ej.set(t,n??e??this[t]),o!==!0||n!==void 0)||(this._$AL.has(t)||(this.hasUpdated||r||(e=void 0),this._$AL.set(t,e)),s===!0&&this._$Em!==t&&(this._$Eq??(this._$Eq=new Set)).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}const t=this.scheduleUpdate();return t!=null&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??(this.renderRoot=this.createRenderRoot()),this._$Ep){for(const[s,o]of this._$Ep)this[s]=o;this._$Ep=void 0}const r=this.constructor.elementProperties;if(r.size>0)for(const[s,o]of r){const{wrapped:n}=o,c=this[s];n!==!0||this._$AL.has(s)||c===void 0||this.C(s,void 0,o,c)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(r=>r.hostUpdate?.()),this.update(e)):this._$EM()}catch(r){throw t=!1,this._$EM(),r}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(e=>e.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&(this._$Eq=this._$Eq.forEach(e=>this._$ET(e,this[e]))),this._$EM()}updated(t){}firstUpdated(t){}};y.elementStyles=[],y.shadowRootOptions={mode:"open"},y[E("elementProperties")]=new Map,y[E("finalized")]=new Map,_t?.({ReactiveElement:y}),(g.reactiveElementVersions??(g.reactiveElementVersions=[])).push("2.1.2");/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const S=globalThis,J=i=>i,M=S.trustedTypes,K=M?M.createPolicy("lit-html",{createHTML:i=>i}):void 0,it="$lit$",v=`lit$${Math.random().toFixed(9).slice(2)}$`,ot="?"+v,yt=`<${ot}>`,_=document,P=()=>_.createComment(""),U=i=>i===null||typeof i!="object"&&typeof i!="function",B=Array.isArray,At=i=>B(i)||typeof i?.[Symbol.iterator]=="function",z=`[
\f\r]`,w=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,Y=/-->/g,G=/>/g,$=RegExp(`>|${z}(?:([^\\s"'>=/]+)(${z}*=${z}*(?:[^
\f\r"'\`<>=]|("|')|))|$)`,"g"),X=/'/g,tt=/"/g,nt=/^(?:script|style|textarea|title)$/i,xt=i=>(t,...e)=>({_$litType$:i,strings:t,values:e}),b=xt(1),A=Symbol.for("lit-noChange"),d=Symbol.for("lit-nothing"),et=new WeakMap,m=_.createTreeWalker(_,129);function at(i,t){if(!B(i)||!i.hasOwnProperty("raw"))throw Error("invalid template strings array");return K!==void 0?K.createHTML(t):t}const wt=(i,t)=>{const e=i.length-1,r=[];let s,o=t===2?"<svg>":t===3?"<math>":"",n=w;for(let c=0;c<e;c++){const a=i[c];let h,p,l=-1,u=0;for(;u<a.length&&(n.lastIndex=u,p=n.exec(a),p!==null);)u=n.lastIndex,n===w?p[1]==="!--"?n=Y:p[1]!==void 0?n=G:p[2]!==void 0?(nt.test(p[2])&&(s=RegExp("</"+p[2],"g")),n=$):p[3]!==void 0&&(n=$):n===$?p[0]===">"?(n=s??w,l=-1):p[1]===void 0?l=-2:(l=n.lastIndex-p[2].length,h=p[1],n=p[3]===void 0?$:p[3]==='"'?tt:X):n===tt||n===X?n=$:n===Y||n===G?n=w:(n=$,s=void 0);const f=n===$&&i[c+1].startsWith("/>")?" ":"";o+=n===w?a+yt:l>=0?(r.push(h),a.slice(0,l)+it+a.slice(l)+v+f):a+v+(l===-2?c:f)}return[at(i,o+(i[e]||"<?>")+(t===2?"</svg>":t===3?"</math>":"")),r]};class R{constructor({strings:t,_$litType$:e},r){let s;this.parts=[];let o=0,n=0;const c=t.length-1,a=this.parts,[h,p]=wt(t,e);if(this.el=R.createElement(h,r),m.currentNode=this.el.content,e===2||e===3){const l=this.el.content.firstChild;l.replaceWith(...l.childNodes)}for(;(s=m.nextNode())!==null&&a.length<c;){if(s.nodeType===1){if(s.hasAttributes())for(const l of s.getAttributeNames())if(l.endsWith(it)){const u=p[n++],f=s.getAttribute(l).split(v),T=/([.?@])?(.*)/.exec(u);a.push({type:1,index:o,name:T[2],strings:f,ctor:T[1]==="."?St:T[1]==="?"?kt:T[1]==="@"?Ct:q}),s.removeAttribute(l)}else l.startsWith(v)&&(a.push({type:6,index:o}),s.removeAttribute(l));if(nt.test(s.tagName)){const l=s.textContent.split(v),u=l.length-1;if(u>0){s.textContent=M?M.emptyScript:"";for(let f=0;f<u;f++)s.append(l[f],P()),m.nextNode(),a.push({type:2,index:++o});s.append(l[u],P())}}}else if(s.nodeType===8)if(s.data===ot)a.push({type:2,index:o});else{let l=-1;for(;(l=s.data.indexOf(v,l+1))!==-1;)a.push({type:7,index:o}),l+=v.length-1}o++}}static createElement(t,e){const r=_.createElement("template");return r.innerHTML=t,r}}function x(i,t,e=i,r){if(t===A)return t;let s=r!==void 0?e._$Co?.[r]:e._$Cl;const o=U(t)?void 0:t._$litDirective$;return s?.constructor!==o&&(s?._$AO?.(!1),o===void 0?s=void 0:(s=new o(i),s._$AT(i,e,r)),r!==void 0?(e._$Co??(e._$Co=[]))[r]=s:e._$Cl=s),s!==void 0&&(t=x(i,s._$AS(i,t.values),s,r)),t}class Et{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:r}=this._$AD,s=(t?.creationScope??_).importNode(e,!0);m.currentNode=s;let o=m.nextNode(),n=0,c=0,a=r[0];for(;a!==void 0;){if(n===a.index){let h;a.type===2?h=new O(o,o.nextSibling,this,t):a.type===1?h=new a.ctor(o,a.name,a.strings,this,t):a.type===6&&(h=new Pt(o,this,t)),this._$AV.push(h),a=r[++c]}n!==a?.index&&(o=m.nextNode(),n++)}return m.currentNode=_,s}p(t){let e=0;for(const r of this._$AV)r!==void 0&&(r.strings!==void 0?(r._$AI(t,r,e),e+=r.strings.length-2):r._$AI(t[e])),e++}}class O{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,r,s){this.type=2,this._$AH=d,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=r,this.options=s,this._$Cv=s?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return e!==void 0&&t?.nodeType===11&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=x(this,t,e),U(t)?t===d||t==null||t===""?(this._$AH!==d&&this._$AR(),this._$AH=d):t!==this._$AH&&t!==A&&this._(t):t._$litType$!==void 0?this.$(t):t.nodeType!==void 0?this.T(t):At(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==d&&U(this._$AH)?this._$AA.nextSibling.data=t:this.T(_.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:r}=t,s=typeof r=="number"?this._$AC(t):(r.el===void 0&&(r.el=R.createElement(at(r.h,r.h[0]),this.options)),r);if(this._$AH?._$AD===s)this._$AH.p(e);else{const o=new Et(s,this),n=o.u(this.options);o.p(e),this.T(n),this._$AH=o}}_$AC(t){let e=et.get(t.strings);return e===void 0&&et.set(t.strings,e=new R(t)),e}k(t){B(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let r,s=0;for(const o of t)s===e.length?e.push(r=new O(this.O(P()),this.O(P()),this,this.options)):r=e[s],r._$AI(o),s++;s<e.length&&(this._$AR(r&&r._$AB.nextSibling,s),e.length=s)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const r=J(t).nextSibling;J(t).remove(),t=r}}setConnected(t){this._$AM===void 0&&(this._$Cv=t,this._$AP?.(t))}}class q{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,r,s,o){this.type=1,this._$AH=d,this._$AN=void 0,this.element=t,this.name=e,this._$AM=s,this.options=o,r.length>2||r[0]!==""||r[1]!==""?(this._$AH=Array(r.length-1).fill(new String),this.strings=r):this._$AH=d}_$AI(t,e=this,r,s){const o=this.strings;let n=!1;if(o===void 0)t=x(this,t,e,0),n=!U(t)||t!==this._$AH&&t!==A,n&&(this._$AH=t);else{const c=t;let a,h;for(t=o[0],a=0;a<o.length-1;a++)h=x(this,c[r+a],e,a),h===A&&(h=this._$AH[a]),n||(n=!U(h)||h!==this._$AH[a]),h===d?t=d:t!==d&&(t+=(h??"")+o[a+1]),this._$AH[a]=h}n&&!s&&this.j(t)}j(t){t===d?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class St extends q{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===d?void 0:t}}class kt extends q{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==d)}}class Ct extends q{constructor(t,e,r,s,o){super(t,e,r,s,o),this.type=5}_$AI(t,e=this){if((t=x(this,t,e,0)??d)===A)return;const r=this._$AH,s=t===d&&r!==d||t.capture!==r.capture||t.once!==r.once||t.passive!==r.passive,o=t!==d&&(r===d||s);s&&this.element.removeEventListener(this.name,this,r),o&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){typeof this._$AH=="function"?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class Pt{constructor(t,e,r){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=r}get _$AU(){return this._$AM._$AU}_$AI(t){x(this,t)}}const Ut=S.litHtmlPolyfillSupport;Ut?.(R,O),(S.litHtmlVersions??(S.litHtmlVersions=[])).push("3.3.3");const Rt=(i,t,e)=>{const r=e?.renderBefore??t;let s=r._$litPart$;if(s===void 0){const o=e?.renderBefore??null;r._$litPart$=s=new O(t.insertBefore(P(),o),o,void 0,e??{})}return s._$AI(i),s};/**
 * @license
 * Copyright 2017 Google LLC
 * SPDX-License-Identifier: BSD-3-Clause
 */const k=globalThis;class C extends y{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){var e;const t=super.createRenderRoot();return(e=this.renderOptions).renderBefore??(e.renderBefore=t.firstChild),t}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=Rt(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return A}}C._$litElement$=!0,C.finalized=!0,k.litElementHydrateSupport?.({LitElement:C});const Ot=k.litElementPolyfillSupport;Ot?.({LitElement:C});(k.litElementVersions??(k.litElementVersions=[])).push("4.2.2");const Tt=[{href:"/",file:"index.html",icon:"dashboard",cat:"admin",title:"Панель управления",sub:"15 разделов: цех, склад, касса, настройки"},{href:"/cashier",file:"cashier.html",icon:"ruble",cat:"tool",lock:!0,title:"Мобильная касса",sub:"Продажа с телефона, код доступа"},{href:"/sbp",file:"sbp.html",icon:"qr",cat:"tool",lock:!0,title:"СБП · входящие",sub:"Платежи по QR, авто-подтверждение"},{href:"/bank",file:"bank.html",icon:"bank",cat:"tool",lock:!0,title:"Поступления из банка",sub:"Выписка и сопоставление с заказами"},{href:"/m",file:"m.html",icon:"printer",cat:"tool",title:"Станок (мобильная)",sub:"Пульты у принтера: старт, пауза, съём"},{href:"/shelf",file:"shelf.html",icon:"shelf",cat:"tv",title:"Экран стеллажа",sub:"Витрина полки для покупателей"},{href:"/tv",file:"tv.html",icon:"tv",cat:"tv",title:"ТВ-дашборд",sub:"Очередь и статусы на большой экран"},{href:"/order",file:"order.html",icon:"cart",cat:"shop",title:"Витрина и заказ",sub:"Каталог, корзина, заявка с телефона"},{href:"/track",file:"track.html",icon:"track",cat:"shop",title:"Статус заказа",sub:"Покупатель следит за готовностью"},{href:"/my",file:"my.html",icon:"user",cat:"shop",title:"Мои заказы",sub:"Личный кабинет покупателя"},{href:"/spool",file:"spool.html",icon:"spool",cat:"tool",title:"Катушки",sub:"Взвешивание и списание пластика"},{href:"/labels",file:"labels.html",icon:"tag",cat:"print",title:"Этикетки",sub:"Печать наклеек и бирок с QR"},{href:"/price-tags",file:"price-tags.html",icon:"tag",cat:"print",title:"Ценники",sub:"Ценники и промостенды 67×32"},{href:"/design",file:"design.html",icon:"pen",cat:"shop",title:"Заявка на дизайн",sub:"Приём индивидуальных заказов"}],D={admin:{label:"Панель",tone:"accent"},tool:{label:"Сотрудникам",tone:"info"},tv:{label:"Экраны",tone:"violet"},shop:{label:"Покупателям",tone:"ok"},print:{label:"Печать",tone:"warn"}};class j extends C{constructor(){super(),this.filter="",this.cat="all",this.qrFor=null}get pages(){const t=this.filter.trim().toLowerCase();return Tt.filter(e=>this.cat!=="all"&&e.cat!==this.cat?!1:t?(e.title+" "+e.sub+" "+e.file).toLowerCase().includes(t):!0)}absUrl(t){return location.origin+t}qrSvg(t){const e=window.QR;return e?e.svg(this.absUrl(t),{ecl:"M",size:240,margin:1,dark:"#0f172a",light:"#ffffff"}):""}iconSvg(t){return window.PFIcons&&window.PFIcons.svg?window.PFIcons.svg(t):""}render(){const t=this.pages;return b`
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
          ${Object.entries(D).map(([e,r])=>b`
            <button type="button" class="hub-cat ${this.cat===e?"on":""}"
                    @click=${()=>this.cat=e}>${r.label}</button>`)}
        </div>
      </div>

      ${t.length===0?b`<div class="hub-empty">Ничего не нашлось — попробуйте другой запрос.</div>`:b`<div class="hub-grid">
            ${t.map(e=>b`
              <a class="pcard" href=${e.href} data-view="">
                <div class="pcard-top">
                  <span class="pcard-ic" .innerHTML=${this.iconSvg(e.icon)||"◈"}></span>
                  <span class="pcard-title">${e.title}</span>
                  <button type="button" class="pcard-qr" title="QR для открытия с телефона"
                          aria-label="QR-код страницы «${e.title}»"
                          @click=${r=>{r.preventDefault(),r.stopPropagation(),this.qrFor=e}}>▦</button>
                </div>
                <span class="pcard-sub">${e.sub}</span>
                <div class="pcard-meta">
                  <span class="badge tone-${D[e.cat].tone}">${D[e.cat].label}</span>
                  ${e.lock?b`<span class="badge lock" title="Требуется код доступа">🔒 код</span>`:""}
                </div>
                <span class="pcard-addr">${e.file}</span>
              </a>`)}
          </div>`}

      ${this.qrFor?b`
        <div class="qr-overlay" role="dialog" aria-modal="true"
             aria-label="QR страницы ${this.qrFor.title}"
             @click=${e=>{e.target===e.currentTarget&&(this.qrFor=null)}}>
          <div class="qr-modal">
            <h3>${this.qrFor.title}</h3>
            <p>Наведите камеру телефона — страница откроется в локальной сети</p>
            <div class="qr-box" .innerHTML=${this.qrSvg(this.qrFor.href)}></div>
            <div class="qr-addr">${this.absUrl(this.qrFor.href)}</div>
            <div class="qr-actions">
              <button type="button" class="btn" @click=${()=>this.qrFor=null}>Закрыть</button>
              <a class="btn primary" href=${this.qrFor.href}>Открыть</a>
            </div>
          </div>
        </div>`:""}
    `}}N(j,"properties",{filter:{state:!0},cat:{state:!0},qrFor:{state:!0}}),N(j,"styles",dt`
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
  `);customElements.define("pf-pages-hub",j);window.PF_DIST_READY=!0;
