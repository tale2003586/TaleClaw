import{c as r,j as a,s as t,J as i,q as n}from"./index-0wNKSh4h.js";/**
 * @license lucide-react v1.27.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const o=[["path",{d:"M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1",key:"ezmyqa"}],["path",{d:"M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1",key:"e1hn23"}]],c=r("braces",o);function h({event:e}){return a.jsxs("div",{className:`trace-event trace-${n(e)}`,children:[a.jsxs("div",{children:[a.jsxs("strong",{children:[a.jsx(c,{"aria-hidden":"true",size:12}),e.event]}),a.jsx("span",{children:t(e)}),a.jsx("small",{children:u(e)||d(e.timestamp)})]}),a.jsx(i,{value:e.payload})]})}function d(e){if(!e)return"-";const s=new Date(e);return Number.isNaN(s.getTime())?e:s.toLocaleString("zh-CN",{hour12:!1})}function u(e){const s=Number(e.payload.duration_ms);return Number.isFinite(s)?s<1e3?`${Math.round(s)}ms`:`${(s/1e3).toFixed(1)}s`:""}export{h as T,d as f};
