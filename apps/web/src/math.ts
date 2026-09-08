/** Normalize common LaTeX delimiters outside Markdown code and existing math. */
export function normalizeMathDelimiters(markdown:string):string {
  let fence:{marker:string;length:number}|null=null
  let display=false
  return markdown.split(/(?<=\n)/).map(line=>{
    const boundary=line.match(/^ {0,3}(`{3,}|~{3,})/)
    if(fence){if(boundary&&boundary[1][0]===fence.marker&&boundary[1].length>=fence.length&&line.slice(boundary[0].length).trim()==='')fence=null;return line}
    if(boundary){fence={marker:boundary[1][0],length:boundary[1].length};return line}
    if(/^(?: {4}|\t)/.test(line)&&!display)return line
    let result='',index=0
    while(index<line.length){
      if(line[index]==='`'){
        const length=line.slice(index).match(/^`+/)![0].length,marker='`'.repeat(length),end=line.indexOf(marker,index+length)
        if(end>=0){result+=line.slice(index,end+length);index=end+length;continue}
      }
      if(line[index]==='\\'){
        if(line[index+1]==='\\'){result+='\\\\';index+=2;continue}
        if(line[index+1]==='['){result+='\n$$\n';display=true;index+=2;continue}
        if(line[index+1]===']'&&display){result+='\n$$\n';display=false;index+=2;continue}
        if(line[index+1]==='('){const end=line.indexOf('\\)',index+2);if(end>=0){result+='$'+line.slice(index+2,end)+'$';index=end+2;continue}}
      }
      result+=line[index++];
    }
    return result
  }).join('')
}
