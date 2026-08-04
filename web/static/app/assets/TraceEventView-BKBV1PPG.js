import{c as r,j as s,l as t,J as i,m as n}from"./index-B_5sl-5c.js";/**
 * @license lucide-react v1.27.0 - ISC
 *
 * This source code is licensed under the ISC license.
 * See the LICENSE file in the root directory of this source tree.
 */const o=[["path",{d:"M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1",key:"ezmyqa"}],["path",{d:"M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1",key:"e1hn23"}]],c=r("braces",o);function h({event:e}){return s.jsxs("div",{className:`trace-event trace-${n(e)}`,children:[s.jsxs("div",{children:[s.jsxs("strong",{children:[s.jsx(c,{"aria-hidden":"true",size:12}),e.event]}),s.jsx("span",{children:t(e)}),s.jsx("small",{children:u(e)||d(e.timestamp)})]}),s.jsx(i,{value:e.payload})]})}function d(e){if(!e)return"-";const a=new Date(e);return Number.isNaN(a.getTime())?e:a.toLocaleString("zh-CN",{hour12:!1})}function u(e){const a=Number(e.payload.duration_ms);return Number.isFinite(a)?a<1e3?`${Math.round(a)}ms`:`${(a/1e3).toFixed(1)}s`:""}export{h as T,d as f};
