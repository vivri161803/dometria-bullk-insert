## 1. I Moduli Applicativi (La Logica di Estrazione)

Ogni blocco deve essere sviluppato come un modulo Python indipendente, in modo da poter essere facilmente incapsulato in un task asincrono.

### **Blocco 1: Preprocessing**

**Obiettivo:** Preparare il testo grezzo per l'estrazione.

**Implementazione:** Utilizza `spaCy` per eseguire il chunking con overlap e il coreference alignment (risoluzione dei riferimenti anaforici).

**Scoring:** Integra una chiamata a un modello LLM leggero e veloce tramite OpenRouter (es. GPT-4o-mini) per assegnare un punteggio semantico di rilevanza a ogni chunk. Questo punteggio deve basarsi esclusivamente sul contenuto del chunk, non sulla sua dimensione.

### **Blocco 2: Estrazione del Contratto**

**Obiettivo:** Determinare lo schema JSON esatto da riempire (il contratto).

**Implementazione:** Applica un approccio multi-turn hybrid RB-RAG. Prima, usa un LLM per estrarre lv1, mentre lv2 e lv3 vengono dedotti tramite matching sul grafo. Confronta questi percorsi con i top-k chunk utilizzando la cosine similarity e seleziona i campi corretti tramite una votazione a maggioranza ponderata.

### **Blocco 3: Estrazione dell'Asset Candidato**

**Obiettivo:** Popolare il contratto con i dati effettivi.

**Implementazione:** Passa il testo precedentemente processato (i chunk con i voti) e il contratto JSON vuoto a un LLM ad alta capacità, come GPT-4o.

**Esecuzione:** L'LLM eseguirà l'estrazione per chunk, restituendo in output l'asset candidato completo in aderenza alla struttura richiesta.


NOTA: usare instructor con pydantic per vincolare gli output del'LLM a d ogni passaggio necessario. Ad esempio per il blocco 1 definire una classe Pydantic apposita per il voto al chunk. Fare la stessa cosa anche per il blocco 2, per determinare il lv1 da un set definito a piori. Fare la stessa cosa per il blocco 3, usando la schema del contratto come schema pydantic.

NOTA: usare pytest e ruff per controllare il codice implementato. Implementanto test di qualità per ogni modulo che verrà eseguiti come pipeline pre-commit.

---

## 2. Architettura Orchestratore-Queue-Worker

Poiché i Blocchi dall'1 al 4 sono computazionalmente pesanti (elaborazione NLP e latenza LLM), un'esecuzione sincrona bloccherebbe le API. Il piano prevede di orchestrare il tutto in background.

### **L'Orchestratore (FastAPI):**
* Ha il solo compito di ricevere la ricezione asincrona delle richieste da Dometria Connect.

* Non appena riceve il payload, impacchetta la richiesta, crea il primo task e lo invia al broker di messaggi. Fatto questo, risponde immediatamente al client (es. `202 Accepted`), svincolandosi dall'attesa.

### **La Coda di Messaggi (Redis):**
* Agisce da intermediario e snodo centrale, orchestrando i task asincroni tra i vari blocchi della pipeline.

### **Il Task Worker (Celery):**
* È il motore che esegue materialmente il lavoro in background per non bloccare l'API.

**Flusso a Catena:** Il worker pesca il task da Redis ed esegue il Blocco 2. Al termine, pubblica il risultato e "triggera" (attiva) automaticamente il task del Blocco 3, e così via fino al Blocco 4.

**Resilienza:** Configura Celery con meccanismi di retry automatico per ogni blocco. In caso di fallimento irreversibile (es. API LLM irraggiungibili), la procedura si blocca al suo stato corrente e viene segnalata per un intervento manuale, evitando la perdita dei dati intermedi.
