# Contexto del Proyecto: Sistema y Gestión Financiera (Evaluación)

## Objetivo
Desarrollar una herramienta de software contable que automatice la generación de estados financieros basándose en transacciones y el Plan Contable General Empresarial (PCGE) de Perú.

## Stack Tecnológico
*   **Base de datos:** SQLite
*   **Backend / Lógica:** Python
*   **Frontend:** Streamlit

## Arquitectura
Patrón Modelo-Vista-Controlador (MVC) para mantener el código modular y ordenado.

## Reglas Estrictas de Desarrollo (Para la IA)
1.  **Validación de Partida Doble:** Es obligatorio validar que la suma de los débitos (Debe) sea igual a la suma de los créditos (Haber) antes de registrar cualquier asiento en SQLite.
2.  **Persistencia:** Está estrictamente prohibido usar diccionarios en RAM para datos transaccionales. Todo debe insertarse y consultarse directamente en SQLite.
3.  **Lógica del PCGE:** La base de datos clasifica las cuentas usando el campo `elemento` (del 1 al 9). La IA debe deducir la naturaleza de la cuenta y a qué estado financiero pertenece leyendo únicamente este `elemento` o el primer dígito del código de la cuenta.
4.  **Integridad:** Mantener estricta integridad referencial en la base de datos mediante Foreign Keys entre Asientos y Detalles.