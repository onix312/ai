var t=Object.defineProperty,e=(e,s,r)=>((e,s,r)=>s in e?t(e,s,{enumerable:!0,configurable:!0,writable:!0,value:r}):e[s]=r)(e,"symbol"!=typeof s?s+"":s,r);const s=globalThis,r=s.ShadowRoot&&(void 0===s.ShadyCSS||s.ShadyCSS.nativeShadow)&&"adoptedStyleSheets"in Document.prototype&&"replace"in CSSStyleSheet.prototype,i=Symbol(),o=new WeakMap;let n=class{constructor(t,e,s){if(this._$cssResult$=!0,s!==i)throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");this.cssText=t,this.t=e}get styleSheet(){let t=this.o;const e=this.t;if(r&&void 0===t){const s=void 0!==e&&1===e.length;s&&(t=o.get(e)),void 0===t&&((this.o=t=new CSSStyleSheet).replaceSync(this.cssText),s&&o.set(e,t))}return t}toString(){return this.cssText}};const a=r?t=>t:t=>t instanceof CSSStyleSheet?(t=>{let e="";for(const s of t.cssRules)e+=s.cssText;return(t=>new n("string"==typeof t?t:t+"",void 0,i))(e)})(t):t,{is:l,defineProperty:c,getOwnPropertyDescriptor:h,getOwnPropertyNames:d,getOwnPropertySymbols:p,getPrototypeOf:u}=Object,f=globalThis,v=f.trustedTypes,b=v?v.emptyScript:"",g=f.reactiveElementPolyfillSupport,$=(t,e)=>t,m={toAttribute(t,e){switch(e){case Boolean:t=t?b:null;break;case Object:case Array:t=null==t?t:JSON.stringify(t)}return t},fromAttribute(t,e){let s=t;switch(e){case Boolean:s=null!==t;break;case Number:s=null===t?null:Number(t);break;case Object:case Array:try{s=JSON.parse(t)}catch(r){s=null}}return s}},_=(t,e)=>!l(t,e),y={attribute:!0,type:String,converter:m,reflect:!1,useDefault:!1,hasChanged:_};Symbol.metadata??(Symbol.metadata=Symbol("metadata")),f.litPropertyMetadata??(f.litPropertyMetadata=new WeakMap);let A=class extends HTMLElement{static addInitializer(t){this._$Ei(),(this.l??(this.l=[])).push(t)}static get observedAttributes(){return this.finalize(),this._$Eh&&[...this._$Eh.keys()]}static createProperty(t,e=y){if(e.state&&(e.attribute=!1),this._$Ei(),this.prototype.hasOwnProperty(t)&&((e=Object.create(e)).wrapped=!0),this.elementProperties.set(t,e),!e.noAccessor){const s=Symbol(),r=this.getPropertyDescriptor(t,s,e);void 0!==r&&c(this.prototype,t,r)}}static getPropertyDescriptor(t,e,s){const{get:r,set:i}=h(this.prototype,t)??{get(){return this[e]},set(t){this[e]=t}};return{get:r,set(e){const o=r?.call(this);i?.call(this,e),this.requestUpdate(t,o,s)},configurable:!0,enumerable:!0}}static getPropertyOptions(t){return this.elementProperties.get(t)??y}static _$Ei(){if(this.hasOwnProperty($("elementProperties")))return;const t=u(this);t.finalize(),void 0!==t.l&&(this.l=[...t.l]),this.elementProperties=new Map(t.elementProperties)}static finalize(){if(this.hasOwnProperty($("finalized")))return;if(this.finalized=!0,this._$Ei(),this.hasOwnProperty($("properties"))){const t=this.properties,e=[...d(t),...p(t)];for(const s of e)this.createProperty(s,t[s])}const t=this[Symbol.metadata];if(null!==t){const e=litPropertyMetadata.get(t);if(void 0!==e)for(const[t,s]of e)this.elementProperties.set(t,s)}this._$Eh=new Map;for(const[e,s]of this.elementProperties){const t=this._$Eu(e,s);void 0!==t&&this._$Eh.set(t,e)}this.elementStyles=this.finalizeStyles(this.styles)}static finalizeStyles(t){const e=[];if(Array.isArray(t)){const s=new Set(t.flat(1/0).reverse());for(const t of s)e.unshift(a(t))}else void 0!==t&&e.push(a(t));return e}static _$Eu(t,e){const s=e.attribute;return!1===s?void 0:"string"==typeof s?s:"string"==typeof t?t.toLowerCase():void 0}constructor(){super(),this._$Ep=void 0,this.isUpdatePending=!1,this.hasUpdated=!1,this._$Em=null,this._$Ev()}_$Ev(){this._$ES=new Promise(t=>this.enableUpdating=t),this._$AL=new Map,this._$E_(),this.requestUpdate(),this.constructor.l?.forEach(t=>t(this))}addController(t){(this._$EO??(this._$EO=new Set)).add(t),void 0!==this.renderRoot&&this.isConnected&&t.hostConnected?.()}removeController(t){this._$EO?.delete(t)}_$E_(){const t=new Map,e=this.constructor.elementProperties;for(const s of e.keys())this.hasOwnProperty(s)&&(t.set(s,this[s]),delete this[s]);t.size>0&&(this._$Ep=t)}createRenderRoot(){const t=this.shadowRoot??this.attachShadow(this.constructor.shadowRootOptions);return((t,e)=>{if(r)t.adoptedStyleSheets=e.map(t=>t instanceof CSSStyleSheet?t:t.styleSheet);else for(const r of e){const e=document.createElement("style"),i=s.litNonce;void 0!==i&&e.setAttribute("nonce",i),e.textContent=r.cssText,t.appendChild(e)}})(t,this.constructor.elementStyles),t}connectedCallback(){this.renderRoot??(this.renderRoot=this.createRenderRoot()),this.enableUpdating(!0),this._$EO?.forEach(t=>t.hostConnected?.())}enableUpdating(t){}disconnectedCallback(){this._$EO?.forEach(t=>t.hostDisconnected?.())}attributeChangedCallback(t,e,s){this._$AK(t,s)}_$ET(t,e){const s=this.constructor.elementProperties.get(t),r=this.constructor._$Eu(t,s);if(void 0!==r&&!0===s.reflect){const i=(void 0!==s.converter?.toAttribute?s.converter:m).toAttribute(e,s.type);this._$Em=t,null==i?this.removeAttribute(r):this.setAttribute(r,i),this._$Em=null}}_$AK(t,e){const s=this.constructor,r=s._$Eh.get(t);if(void 0!==r&&this._$Em!==r){const t=s.getPropertyOptions(r),i="function"==typeof t.converter?{fromAttribute:t.converter}:void 0!==t.converter?.fromAttribute?t.converter:m;this._$Em=r;const o=i.fromAttribute(e,t.type);this[r]=o??this._$Ej?.get(r)??o,this._$Em=null}}requestUpdate(t,e,s,r=!1,i){if(void 0!==t){const o=this.constructor;if(!1===r&&(i=this[t]),s??(s=o.getPropertyOptions(t)),!((s.hasChanged??_)(i,e)||s.useDefault&&s.reflect&&i===this._$Ej?.get(t)&&!this.hasAttribute(o._$Eu(t,s))))return;this.C(t,e,s)}!1===this.isUpdatePending&&(this._$ES=this._$EP())}C(t,e,{useDefault:s,reflect:r,wrapped:i},o){s&&!(this._$Ej??(this._$Ej=new Map)).has(t)&&(this._$Ej.set(t,o??e??this[t]),!0!==i||void 0!==o)||(this._$AL.has(t)||(this.hasUpdated||s||(e=void 0),this._$AL.set(t,e)),!0===r&&this._$Em!==t&&(this._$Eq??(this._$Eq=new Set)).add(t))}async _$EP(){this.isUpdatePending=!0;try{await this._$ES}catch(e){Promise.reject(e)}const t=this.scheduleUpdate();return null!=t&&await t,!this.isUpdatePending}scheduleUpdate(){return this.performUpdate()}performUpdate(){if(!this.isUpdatePending)return;if(!this.hasUpdated){if(this.renderRoot??(this.renderRoot=this.createRenderRoot()),this._$Ep){for(const[t,e]of this._$Ep)this[t]=e;this._$Ep=void 0}const t=this.constructor.elementProperties;if(t.size>0)for(const[e,s]of t){const{wrapped:t}=s,r=this[e];!0!==t||this._$AL.has(e)||void 0===r||this.C(e,void 0,s,r)}}let t=!1;const e=this._$AL;try{t=this.shouldUpdate(e),t?(this.willUpdate(e),this._$EO?.forEach(t=>t.hostUpdate?.()),this.update(e)):this._$EM()}catch(s){throw t=!1,this._$EM(),s}t&&this._$AE(e)}willUpdate(t){}_$AE(t){this._$EO?.forEach(t=>t.hostUpdated?.()),this.hasUpdated||(this.hasUpdated=!0,this.firstUpdated(t)),this.updated(t)}_$EM(){this._$AL=new Map,this.isUpdatePending=!1}get updateComplete(){return this.getUpdateComplete()}getUpdateComplete(){return this._$ES}shouldUpdate(t){return!0}update(t){this._$Eq&&(this._$Eq=this._$Eq.forEach(t=>this._$ET(t,this[t]))),this._$EM()}updated(t){}firstUpdated(t){}};A.elementStyles=[],A.shadowRootOptions={mode:"open"},A[$("elementProperties")]=new Map,A[$("finalized")]=new Map,g?.({ReactiveElement:A}),(f.reactiveElementVersions??(f.reactiveElementVersions=[])).push("2.1.2");const w=globalThis,x=t=>t,E=w.trustedTypes,S=E?E.createPolicy("lit-html",{createHTML:t=>t}):void 0,k="$lit$",P=`lit$${Math.random().toFixed(9).slice(2)}$`,C="?"+P,U=`<${C}>`,R=document,q=()=>R.createComment(""),O=t=>null===t||"object"!=typeof t&&"function"!=typeof t,H=Array.isArray,M="[ \t\n\f\r]",T=/<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g,N=/-->/g,z=/>/g,I=RegExp(`>|${M}(?:([^\\s"'>=/]+)(${M}*=${M}*(?:[^ \t\n\f\r"'\`<>=]|("|')|))|$)`,"g"),j=/'/g,D=/"/g,L=/^(?:script|style|textarea|title)$/i,F=(J=1,(t,...e)=>({_$litType$:J,strings:t,values:e})),B=Symbol.for("lit-noChange"),Q=Symbol.for("lit-nothing"),V=new WeakMap,W=R.createTreeWalker(R,129);var J;function K(t,e){if(!H(t)||!t.hasOwnProperty("raw"))throw Error("invalid template strings array");return void 0!==S?S.createHTML(e):e}class Y{constructor({strings:t,_$litType$:e},s){let r;this.parts=[];let i=0,o=0;const n=t.length-1,a=this.parts,[l,c]=((t,e)=>{const s=t.length-1,r=[];let i,o=2===e?"<svg>":3===e?"<math>":"",n=T;for(let a=0;a<s;a++){const e=t[a];let s,l,c=-1,h=0;for(;h<e.length&&(n.lastIndex=h,l=n.exec(e),null!==l);)h=n.lastIndex,n===T?"!--"===l[1]?n=N:void 0!==l[1]?n=z:void 0!==l[2]?(L.test(l[2])&&(i=RegExp("</"+l[2],"g")),n=I):void 0!==l[3]&&(n=I):n===I?">"===l[0]?(n=i??T,c=-1):void 0===l[1]?c=-2:(c=n.lastIndex-l[2].length,s=l[1],n=void 0===l[3]?I:'"'===l[3]?D:j):n===D||n===j?n=I:n===N||n===z?n=T:(n=I,i=void 0);const d=n===I&&t[a+1].startsWith("/>")?" ":"";o+=n===T?e+U:c>=0?(r.push(s),e.slice(0,c)+k+e.slice(c)+P+d):e+P+(-2===c?a:d)}return[K(t,o+(t[s]||"<?>")+(2===e?"</svg>":3===e?"</math>":"")),r]})(t,e);if(this.el=Y.createElement(l,s),W.currentNode=this.el.content,2===e||3===e){const t=this.el.content.firstChild;t.replaceWith(...t.childNodes)}for(;null!==(r=W.nextNode())&&a.length<n;){if(1===r.nodeType){if(r.hasAttributes())for(const t of r.getAttributeNames())if(t.endsWith(k)){const e=c[o++],s=r.getAttribute(t).split(P),n=/([.?@])?(.*)/.exec(e);a.push({type:1,index:i,name:n[2],strings:s,ctor:"."===n[1]?et:"?"===n[1]?st:"@"===n[1]?rt:tt}),r.removeAttribute(t)}else t.startsWith(P)&&(a.push({type:6,index:i}),r.removeAttribute(t));if(L.test(r.tagName)){const t=r.textContent.split(P),e=t.length-1;if(e>0){r.textContent=E?E.emptyScript:"";for(let s=0;s<e;s++)r.append(t[s],q()),W.nextNode(),a.push({type:2,index:++i});r.append(t[e],q())}}}else if(8===r.nodeType)if(r.data===C)a.push({type:2,index:i});else{let t=-1;for(;-1!==(t=r.data.indexOf(P,t+1));)a.push({type:7,index:i}),t+=P.length-1}i++}}static createElement(t,e){const s=R.createElement("template");return s.innerHTML=t,s}}function Z(t,e,s=t,r){if(e===B)return e;let i=void 0!==r?s._$Co?.[r]:s._$Cl;const o=O(e)?void 0:e._$litDirective$;return i?.constructor!==o&&(i?._$AO?.(!1),void 0===o?i=void 0:(i=new o(t),i._$AT(t,s,r)),void 0!==r?(s._$Co??(s._$Co=[]))[r]=i:s._$Cl=i),void 0!==i&&(e=Z(t,i._$AS(t,e.values),i,r)),e}class G{constructor(t,e){this._$AV=[],this._$AN=void 0,this._$AD=t,this._$AM=e}get parentNode(){return this._$AM.parentNode}get _$AU(){return this._$AM._$AU}u(t){const{el:{content:e},parts:s}=this._$AD,r=(t?.creationScope??R).importNode(e,!0);W.currentNode=r;let i=W.nextNode(),o=0,n=0,a=s[0];for(;void 0!==a;){if(o===a.index){let e;2===a.type?e=new X(i,i.nextSibling,this,t):1===a.type?e=new a.ctor(i,a.name,a.strings,this,t):6===a.type&&(e=new it(i,this,t)),this._$AV.push(e),a=s[++n]}o!==a?.index&&(i=W.nextNode(),o++)}return W.currentNode=R,r}p(t){let e=0;for(const s of this._$AV)void 0!==s&&(void 0!==s.strings?(s._$AI(t,s,e),e+=s.strings.length-2):s._$AI(t[e])),e++}}class X{get _$AU(){return this._$AM?._$AU??this._$Cv}constructor(t,e,s,r){this.type=2,this._$AH=Q,this._$AN=void 0,this._$AA=t,this._$AB=e,this._$AM=s,this.options=r,this._$Cv=r?.isConnected??!0}get parentNode(){let t=this._$AA.parentNode;const e=this._$AM;return void 0!==e&&11===t?.nodeType&&(t=e.parentNode),t}get startNode(){return this._$AA}get endNode(){return this._$AB}_$AI(t,e=this){t=Z(this,t,e),O(t)?t===Q||null==t||""===t?(this._$AH!==Q&&this._$AR(),this._$AH=Q):t!==this._$AH&&t!==B&&this._(t):void 0!==t._$litType$?this.$(t):void 0!==t.nodeType?this.T(t):(t=>H(t)||"function"==typeof t?.[Symbol.iterator])(t)?this.k(t):this._(t)}O(t){return this._$AA.parentNode.insertBefore(t,this._$AB)}T(t){this._$AH!==t&&(this._$AR(),this._$AH=this.O(t))}_(t){this._$AH!==Q&&O(this._$AH)?this._$AA.nextSibling.data=t:this.T(R.createTextNode(t)),this._$AH=t}$(t){const{values:e,_$litType$:s}=t,r="number"==typeof s?this._$AC(t):(void 0===s.el&&(s.el=Y.createElement(K(s.h,s.h[0]),this.options)),s);if(this._$AH?._$AD===r)this._$AH.p(e);else{const t=new G(r,this),s=t.u(this.options);t.p(e),this.T(s),this._$AH=t}}_$AC(t){let e=V.get(t.strings);return void 0===e&&V.set(t.strings,e=new Y(t)),e}k(t){H(this._$AH)||(this._$AH=[],this._$AR());const e=this._$AH;let s,r=0;for(const i of t)r===e.length?e.push(s=new X(this.O(q()),this.O(q()),this,this.options)):s=e[r],s._$AI(i),r++;r<e.length&&(this._$AR(s&&s._$AB.nextSibling,r),e.length=r)}_$AR(t=this._$AA.nextSibling,e){for(this._$AP?.(!1,!0,e);t!==this._$AB;){const e=x(t).nextSibling;x(t).remove(),t=e}}setConnected(t){void 0===this._$AM&&(this._$Cv=t,this._$AP?.(t))}}class tt{get tagName(){return this.element.tagName}get _$AU(){return this._$AM._$AU}constructor(t,e,s,r,i){this.type=1,this._$AH=Q,this._$AN=void 0,this.element=t,this.name=e,this._$AM=r,this.options=i,s.length>2||""!==s[0]||""!==s[1]?(this._$AH=Array(s.length-1).fill(new String),this.strings=s):this._$AH=Q}_$AI(t,e=this,s,r){const i=this.strings;let o=!1;if(void 0===i)t=Z(this,t,e,0),o=!O(t)||t!==this._$AH&&t!==B,o&&(this._$AH=t);else{const r=t;let n,a;for(t=i[0],n=0;n<i.length-1;n++)a=Z(this,r[s+n],e,n),a===B&&(a=this._$AH[n]),o||(o=!O(a)||a!==this._$AH[n]),a===Q?t=Q:t!==Q&&(t+=(a??"")+i[n+1]),this._$AH[n]=a}o&&!r&&this.j(t)}j(t){t===Q?this.element.removeAttribute(this.name):this.element.setAttribute(this.name,t??"")}}class et extends tt{constructor(){super(...arguments),this.type=3}j(t){this.element[this.name]=t===Q?void 0:t}}class st extends tt{constructor(){super(...arguments),this.type=4}j(t){this.element.toggleAttribute(this.name,!!t&&t!==Q)}}class rt extends tt{constructor(t,e,s,r,i){super(t,e,s,r,i),this.type=5}_$AI(t,e=this){if((t=Z(this,t,e,0)??Q)===B)return;const s=this._$AH,r=t===Q&&s!==Q||t.capture!==s.capture||t.once!==s.once||t.passive!==s.passive,i=t!==Q&&(s===Q||r);r&&this.element.removeEventListener(this.name,this,s),i&&this.element.addEventListener(this.name,this,t),this._$AH=t}handleEvent(t){"function"==typeof this._$AH?this._$AH.call(this.options?.host??this.element,t):this._$AH.handleEvent(t)}}class it{constructor(t,e,s){this.element=t,this.type=6,this._$AN=void 0,this._$AM=e,this.options=s}get _$AU(){return this._$AM._$AU}_$AI(t){Z(this,t)}}const ot=w.litHtmlPolyfillSupport;ot?.(Y,X),(w.litHtmlVersions??(w.litHtmlVersions=[])).push("3.3.3");const nt=globalThis;class at extends A{constructor(){super(...arguments),this.renderOptions={host:this},this._$Do=void 0}createRenderRoot(){var t;const e=super.createRenderRoot();return(t=this.renderOptions).renderBefore??(t.renderBefore=e.firstChild),e}update(t){const e=this.render();this.hasUpdated||(this.renderOptions.isConnected=this.isConnected),super.update(t),this._$Do=((t,e,s)=>{const r=s?.renderBefore??e;let i=r._$litPart$;if(void 0===i){const t=s?.renderBefore??null;r._$litPart$=i=new X(e.insertBefore(q(),t),t,void 0,s??{})}return i._$AI(t),i})(e,this.renderRoot,this.renderOptions)}connectedCallback(){super.connectedCallback(),this._$Do?.setConnected(!0)}disconnectedCallback(){super.disconnectedCallback(),this._$Do?.setConnected(!1)}render(){return B}}at._$litElement$=!0,at.finalized=!0,nt.litElementHydrateSupport?.({LitElement:at});const lt=nt.litElementPolyfillSupport;lt?.({LitElement:at}),(nt.litElementVersions??(nt.litElementVersions=[])).push("4.2.2");const ct=[{href:"/",file:"index.html",icon:"dashboard",cat:"admin",title:"Панель управления",sub:"15 разделов: цех, склад, касса, настройки"},{href:"/cashier",file:"cashier.html",icon:"ruble",cat:"tool",lock:!0,title:"Мобильная касса",sub:"Продажа с телефона, код доступа"},{href:"/sbp",file:"sbp.html",icon:"qr",cat:"tool",lock:!0,title:"СБП · входящие",sub:"Платежи по QR, авто-подтверждение"},{href:"/bank",file:"bank.html",icon:"bank",cat:"tool",lock:!0,title:"Поступления из банка",sub:"Выписка и сопоставление с заказами"},{href:"/m",file:"m.html",icon:"printer",cat:"tool",title:"Станок (мобильная)",sub:"Пульты у принтера: старт, пауза, съём"},{href:"/shelf",file:"shelf.html",icon:"shelf",cat:"tv",title:"Экран стеллажа",sub:"Витрина полки для покупателей"},{href:"/tv",file:"tv.html",icon:"tv",cat:"tv",title:"ТВ-дашборд",sub:"Очередь и статусы на большой экран"},{href:"/order",file:"order.html",icon:"cart",cat:"shop",title:"Витрина и заказ",sub:"Каталог, корзина, заявка с телефона"},{href:"/track",file:"track.html",icon:"track",cat:"shop",title:"Статус заказа",sub:"Покупатель следит за готовностью"},{href:"/my",file:"my.html",icon:"user",cat:"shop",title:"Мои заказы",sub:"Личный кабинет покупателя"},{href:"/spool",file:"spool.html",icon:"spool",cat:"tool",title:"Катушки",sub:"Взвешивание и списание пластика"},{href:"/labels",file:"labels.html",icon:"tag",cat:"print",title:"Этикетки",sub:"Печать наклеек и бирок с QR"},{href:"/price-tags",file:"price-tags.html",icon:"tag",cat:"print",title:"Ценники",sub:"Ценники и промостенды 67×32"},{href:"/design",file:"design.html",icon:"pen",cat:"shop",title:"Заявка на дизайн",sub:"Приём индивидуальных заказов"}],ht={admin:{label:"Панель",tone:"accent"},tool:{label:"Сотрудникам",tone:"info"},tv:{label:"Экраны",tone:"violet"},shop:{label:"Покупателям",tone:"ok"},print:{label:"Печать",tone:"warn"}};class dt extends at{constructor(){super(),this.filter="",this.cat="all",this.qrFor=null}get pages(){const t=this.filter.trim().toLowerCase();return ct.filter(e=>("all"===this.cat||e.cat===this.cat)&&(!t||(e.title+" "+e.sub+" "+e.file).toLowerCase().includes(t)))}absUrl(t){return location.origin+t}firstUpdated(){let t=0;const e=setInterval(()=>{t++;const s=!(!window.QR||!window.QR.svg),r=!(!window.PFIcons||!window.PFIcons.svg);(s&&r||t>20)&&(clearInterval(e),this.requestUpdate())},250)}qrSvg(t){try{const e=window.QR;return e&&"function"==typeof e.svg?e.svg(this.absUrl(t),{ecl:"M",size:240,margin:1,dark:"#0f172a",light:"#ffffff"}):""}catch(e){return""}}iconSvg(t){try{if(window.PFIcons&&"function"==typeof window.PFIcons.svg){const e=window.PFIcons.svg(t);if(e)return e}}catch(e){}return`<span style="font-size:18px">${{dashboard:"◫",ruble:"₽",qr:"▦",bank:"🏦",printer:"🖨",shelf:"📦",tv:"📺",cart:"🛒",track:"📍",user:"👤",spool:"🧵",tag:"🏷",pen:"✏"}[t]||"◈"}</span>`}qrAvailable(){return!(!window.QR||"function"!=typeof window.QR.svg)}render(){const t=this.pages;return F`
      <div class="hub-toolbar">
        <label class="hub-search">
          <span aria-hidden="true">⌕</span>
          <input type="search" placeholder="Найти страницу: касса, трек, полка…"
                 .value=${this.filter}
                 @input=${t=>this.filter=t.target.value}
                 aria-label="Поиск по страницам">
        </label>
        <div class="hub-cats" role="group" aria-label="Фильтр страниц">
          <button type="button" class="hub-cat ${"all"===this.cat?"on":""}"
                  @click=${()=>this.cat="all"}>Все</button>
          ${Object.entries(ht).map(([t,e])=>F`
            <button type="button" class="hub-cat ${this.cat===t?"on":""}"
                    @click=${()=>this.cat=t}>${e.label}</button>`)}
        </div>
      </div>

      ${0===t.length?F`<div class="hub-empty">Ничего не нашлось — попробуйте другой запрос.</div>`:F`<div class="hub-grid">
            ${t.map(t=>F`
              <a class="pcard" href=${t.href} data-view="">
                <div class="pcard-top">
                  <span class="pcard-ic" .innerHTML=${this.iconSvg(t.icon)||"◈"}></span>
                  <span class="pcard-title">${t.title}</span>
                  <button type="button" class="pcard-qr" title="QR для открытия с телефона"
                          aria-label="QR-код страницы «${t.title}»"
                          @click=${e=>{e.preventDefault(),e.stopPropagation(),this.qrFor=t}}>▦</button>
                </div>
                <span class="pcard-sub">${t.sub}</span>
                <div class="pcard-meta">
                  <span class="badge tone-${ht[t.cat].tone}">${ht[t.cat].label}</span>
                  ${t.lock?F`<span class="badge lock" title="Требуется код доступа">🔒 код</span>`:""}
                </div>
                <span class="pcard-addr">${t.file}</span>
              </a>`)}
          </div>`}

      ${this.qrFor?F`
        <div class="qr-overlay" role="dialog" aria-modal="true"
             aria-label="QR страницы ${this.qrFor.title}"
             @click=${t=>{t.target===t.currentTarget&&(this.qrFor=null)}}>
          <div class="qr-modal">
            <h3>${this.qrFor.title}</h3>
            <p>Наведите камеру телефона — страница откроется в локальной сети</p>
            ${this.qrAvailable()?F`<div class="qr-box" .innerHTML=${this.qrSvg(this.qrFor.href)}></div>`:F`<div class="qr-box" style="display:grid;place-items:center;color:var(--muted);font-size:13px;line-height:1.4">QR-генератор загружается…<br>обновление страницы помогает</div>`}
            <div class="qr-addr">${this.absUrl(this.qrFor.href)}</div>
            <div class="qr-actions">
              <button type="button" class="btn" @click=${()=>this.qrFor=null}>Закрыть</button>
              <a class="btn primary" href=${this.qrFor.href}>Открыть</a>
            </div>
          </div>
        </div>`:""}
    `}}e(dt,"properties",{filter:{state:!0},cat:{state:!0},qrFor:{state:!0}}),e(dt,"styles",((t,...e)=>{const s=1===t.length?t[0]:e.reduce((e,s,r)=>e+(t=>{if(!0===t._$cssResult$)return t.cssText;if("number"==typeof t)return t;throw Error("Value passed to 'css' function must be a 'css' function result: "+t+". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.")})(s)+t[r+1],t[0]);return new n(s,t,i)})`
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
  `),customElements.define("pf-pages-hub",dt),window.PF_DIST_READY=!0;
