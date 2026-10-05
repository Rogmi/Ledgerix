path='app.py'
with open(path,encoding='utf-8',errors='replace') as f:
    s=f.read()
old='                                    cargar_borrador([{"fecha": datetime.date.today().isoformat(), "glosa": glosa_corta, "asiento": resultado_ia}], "dictado por voz")'
if old in s:
    new='                                    fecha_voz = resultado_ia[0].get("fecha", "") if isinstance(resultado_ia, list) and resultado_ia and isinstance(resultado_ia[0], dict) else ""\n                                    cargar_borrador([{"fecha": fecha_voz, "glosa": glosa_corta, "asiento": resultado_ia}], "dictado por voz")'
    s=s.replace(old,new)
    with open(path,'w',encoding='utf-8') as f:
        f.write(s)
    print('ok')
else:
    print('no')
