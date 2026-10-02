import streamlit as st
import pandas as pd
import logica as lg # Conectamos nuestro motor financiero
import datetime

# 1. CONFIGURACIÓN DE LA PÁGINA (Debe ser la primera línea de código)
st.set_page_config(
    page_title="Ledgerix | Sistema Contable",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# 2. BARRA LATERAL (NAVEGACIÓN CORPORATIVA)
st.sidebar.title("Ledgerix 📈")
st.sidebar.markdown("---")
menu = st.sidebar.radio(
    "Navegación del Sistema",
    ["📊 Dashboard Gerencial", "✍️ Registro de Transacciones", "📑 Estados Financieros"]
)
st.sidebar.markdown("---")
st.sidebar.caption("Ledgerix SaaS - Versión 1.0")

# 3. ENRUTAMIENTO DE MÓDULOS

if menu == "📊 Dashboard Gerencial":
    st.title("Dashboard Gerencial")
    st.markdown("Visión general del estado financiero en tiempo real.")
    
    # Llamamos a nuestro motor de lógica
    df_saldos = lg.obtener_saldos_cuentas()
    
    total_activos = 0.0
    total_pasivos = 0.0
    total_patrimonio = 0.0
    utilidad = 0.0
    
    if not df_saldos.empty:
        # Filtramos matemáticamente usando el campo 'elemento' del PCGE
        total_activos = df_saldos[df_saldos['elemento'].isin([1, 2, 3])]['saldo'].sum()
        total_pasivos = df_saldos[df_saldos['elemento'] == 4]['saldo'].sum()
        total_patrimonio = df_saldos[df_saldos['elemento'] == 5]['saldo'].sum()
        
        ingresos = df_saldos[df_saldos['elemento'] == 7]['saldo'].sum()
        gastos = df_saldos[df_saldos['elemento'].isin([6, 9])]['saldo'].sum()
        utilidad = ingresos - gastos
    
    # Tarjetas de Métricas (KPIs)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric(label="Activos Totales", value=f"S/ {total_activos:,.2f}")
    col2.metric(label="Pasivos Totales", value=f"S/ {total_pasivos:,.2f}")
    col3.metric(label="Patrimonio", value=f"S/ {total_patrimonio:,.2f}")
    col4.metric(label="Utilidad del Ejercicio", value=f"S/ {utilidad:,.2f}")
    
    st.markdown("---")
    st.subheader("Saldos Actuales por Cuenta")
    
    if not df_saldos.empty:
        # Mostramos una tabla estilizada con los datos reales
        st.dataframe(
            df_saldos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), 
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No hay transacciones registradas todavía.")

elif menu == "✍️ Registro de Transacciones":
    st.title("Registro de Transacciones")
    st.markdown("Ingresa nuevos asientos contables mediante captura manual o el Asistente IA.")
    
    # Redujimos las pestañas a 2: Manual y el Motor Central IA
    tab_manual, tab_ia = st.tabs(["✍️ Ingreso Manual", "🤖 Asistente IA (Gemini)"])
    
    with tab_manual:
        st.subheader("Ingreso Manual de Asientos")
        
        conn = lg.obtener_conexion()
        cuentas_df = pd.read_sql_query("SELECT codigo || ' - ' || descripcion as nombre_cuenta FROM Cuentas", conn)
        lista_cuentas = cuentas_df['nombre_cuenta'].tolist()
        conn.close()

        if "form_key" not in st.session_state:
            st.session_state.form_key = 0

        with st.form(f"form_asiento_manual_{st.session_state.form_key}", clear_on_submit=False):
            col1, col2 = st.columns([1, 3])
            with col1:
                fecha_input = st.date_input("Fecha de la transacción",min_value=datetime.date(2000, 1, 1))
            with col2:
                glosa_input = st.text_input("Glosa / Descripción de la operación", placeholder="Ej. Por el aporte de capital inicial")
            
            st.markdown("**Detalle del Asiento (Partida Doble)**")
            
            df_inicial = pd.DataFrame([{"Cuenta": None, "Debe": 0.0, "Haber": 0.0} for _ in range(2)])
            df_editado = st.data_editor(
                df_inicial,
                column_config={
                    "Cuenta": st.column_config.SelectboxColumn("Cuenta Contable (PCGE)", options=lista_cuentas, required=True, width="large"),
                    "Debe": st.column_config.NumberColumn("Debe (S/)", min_value=0.0, format="%.2f"),
                    "Haber": st.column_config.NumberColumn("Haber (S/)", min_value=0.0, format="%.2f")
                },
                num_rows="dynamic", 
                use_container_width=True
            )
            
            submit_btn = st.form_submit_button("Registrar Asiento", type="primary")
            
        if submit_btn:
            if not glosa_input:
                st.error("⚠️ La glosa es obligatoria.")
            else:
                detalles_asiento = []
                for index, row in df_editado.iterrows():
                    if pd.notna(row['Cuenta']): 
                        codigo_cuenta = row['Cuenta'].split(" - ")[0] 
                        val_debe = float(row['Debe']) if pd.notna(row['Debe']) else 0.0
                        val_haber = float(row['Haber']) if pd.notna(row['Haber']) else 0.0
                        
                        detalles_asiento.append({'cuenta': codigo_cuenta, 'debe': val_debe, 'haber': val_haber})
                        
                if len(detalles_asiento) < 2:
                    st.error("❌ El asiento debe tener al menos dos movimientos.")
                else:
                    try:
                        exito, mensaje = lg.registrar_asiento_completo(fecha_input, glosa_input, detalles_asiento)
                        if exito:
                            st.success(f"✅ {mensaje}")
                            st.balloons() 
                            import time
                            time.sleep(1.5)
                            st.session_state.form_key += 1 
                            st.rerun()
                    except ValueError as ve:
                        st.error(f"⚠️ {ve}") 
                    except Exception as e:
                        st.error(f"❌ Error del sistema: {e}")

    with tab_ia:
        st.subheader("🤖 Asistente IA Contable (Gemini)")
        st.markdown("La Inteligencia Artificial analizará el contexto, identificará las cuentas del PCGE y calculará la partida doble automáticamente.")
        
        ia_metodo = st.radio(
            "Selecciona el método de captura:", 
            ["📝 Enunciado de Texto", "🎙️ Dictado por Voz", "📸 Escáner Visual"], 
            horizontal=True
        )
        st.markdown("---")
        
        if ia_metodo == "📝 Enunciado de Texto":
            st.info("Pega aquí el caso del profesor (incluso copiando celdas de Excel) y la IA extraerá las transacciones.")
            enunciado_input = st.text_area(
                "Enunciado contable:", 
                placeholder="Ej: Se compra 50,000 de mercadería al contado...",
                height=100
            )
            if st.button("Analizar y Generar Asiento", type="primary", use_container_width=True):
                if enunciado_input:
                    st.info("Próximamente: Conexión con Gemini API.")
                else:
                    st.error("Por favor, ingresa un enunciado.")
                    
        elif ia_metodo == "🎙️ Dictado por Voz":
            st.markdown("**Reconocimiento de Voz a Texto**")
            if st.button("🎤 Iniciar Grabación (Simulación)", type="secondary", use_container_width=True):
                st.warning("Próximamente: Integración del micrófono web.")
                
        elif ia_metodo == "📸 Escáner Visual":
            st.markdown("**Visión Artificial y OCR**")
            foto_input = st.file_uploader("Sube una foto de la diapositiva o factura:", type=["png", "jpg", "jpeg"])
            if foto_input and st.button("Escanear Imagen y Extraer Asiento", type="primary", use_container_width=True):
                st.info("Próximamente: Conexión con Gemini Vision.")

elif menu == "📑 Estados Financieros":
    st.title("Estados Financieros")
    st.markdown("Reportes automáticos basados en el Plan Contable General Empresarial (PCGE).")
    
    df_saldos = lg.obtener_saldos_cuentas()
    
    tab_balance, tab_resultados = st.tabs(["Balance General", "Estado de Resultados"])
    
    with tab_balance:
        st.subheader("Estado de Situación Financiera")
        
        if not df_saldos.empty:
            # Separamos las cuentas según el PCGE
            activos = df_saldos[df_saldos['elemento'].isin([1, 2, 3])]
            pasivos_patrimonio = df_saldos[df_saldos['elemento'].isin([4, 5])]
            
            # Calculamos la utilidad del periodo para cuadrar el balance
            ingresos_tot = df_saldos[df_saldos['elemento'] == 7]['saldo'].sum()
            gastos_tot = df_saldos[df_saldos['elemento'].isin([6, 9])]['saldo'].sum()
            utilidad = ingresos_tot - gastos_tot
            
            col_activo, col_pasivo = st.columns(2)
            
            with col_activo:
                st.markdown("### Activos")
                st.dataframe(activos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.success(f"**Total Activos: S/ {activos['saldo'].sum():,.2f}**")
                
            with col_pasivo:
                st.markdown("### Pasivos y Patrimonio")
                st.dataframe(pasivos_patrimonio[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                if utilidad != 0:
                    st.caption(f"*Utilidad del Ejercicio a distribuir: S/ {utilidad:,.2f}*")
                
                total_p_y_p = pasivos_patrimonio['saldo'].sum() + utilidad
                st.error(f"**Total Pasivo + Patrimonio: S/ {total_p_y_p:,.2f}**")
                
        else:
            st.info("Aún no hay registros para procesar el Balance General.")
            
    with tab_resultados:
        st.subheader("Estado de Resultados Integrales")
        
        if not df_saldos.empty:
            ingresos = df_saldos[df_saldos['elemento'] == 7]
            gastos = df_saldos[df_saldos['elemento'].isin([6, 9])]
            
            col_ing, col_gas = st.columns(2)
            
            with col_ing:
                st.markdown("### Ingresos")
                if not ingresos.empty:
                    st.dataframe(ingresos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.success(f"**Total Ingresos: S/ {ingresos_tot:,.2f}**")
                
            with col_gas:
                st.markdown("### Gastos")
                if not gastos.empty:
                    st.dataframe(gastos[['codigo', 'descripcion', 'saldo']].style.format({'saldo': 'S/ {:.2f}'}), hide_index=True, use_container_width=True)
                st.error(f"**Total Gastos: S/ {gastos_tot:,.2f}**")
            
            st.markdown("---")
            color_utilidad = "normal" if utilidad >= 0 else "inverse"
            st.metric(label="RESULTADO DEL EJERCICIO (Utilidad / Pérdida)", value=f"S/ {utilidad:,.2f}", delta_color=color_utilidad)
        else:
            st.info("Aún no hay registros de ingresos o gastos para procesar.")