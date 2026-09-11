// Synthetic, sanitized fixture matching the shapes observed in Weex bundles.
const parser={encodeUrl:"https://parser.example.invalid/v1/lua/encode",decodeUrl:"/v1/lua/decode"};
function setDhw(next,last){return bridge.luaControl({params:Object.assign({},last.base,{control_type:"base",dhw_temp_set:next,dhw_power_state:"on"})})}
function setDhwAgain(next,last){return bridge.luaControl({params:Object.assign({},last.base,{control_type:"base",dhw_temp_set:next,dhw_power_state:"on"})})}
function queryBase(){return bridge.luaQuery({params:{query_type:"base",fields:["dhw_temp_set","dhw_power_state"]}})}
const dhwLimits={dhw_temp_set:{min:20,max:65,step:1}};
const powerValues={dhw_power_state:["off","on"]};
function dynamic(kind,value){return bridge.luaControl({params:{control_type:kind,[fieldName]:value}})}
