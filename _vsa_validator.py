import os
import streamlit as st
import datetime
import uuid
import logging
from langchain.schema import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain.vectorstores import FAISS
from langchain.schema import Document
from langchain_openai import OpenAIEmbeddings
from langchain.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langgraph.graph import START, END, StateGraph
from langchain_core.prompts import ChatPromptTemplate
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_community.document_loaders import WebBaseLoader, PyPDFLoader
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_core.pydantic_v1 import BaseModel, Field
from typing import List, Dict
from typing_extensions import TypedDict


# Initialize Groq LLM
llm = ChatGoogleGenerativeAI(model="gemini-1.5-flash", temperature=0.0)


# Initialize vector store and retriever
def load_documents(sources: List[str]) -> List:
    docs = []
    for source in sources:
        if source.startswith('http'):
            loader = WebBaseLoader(source)
        elif source.endswith('.pdf'):
            loader = PyPDFLoader(source)
        else:
            raise ValueError(f"Unsupported source type: {source}")
        docs.extend(loader.load())
    return docs

def create_or_load_vectorstore(sources: List[str], index_name: str = "index_vsa") -> FAISS:
    # Get the current working directory
    current_dir = os.getcwd()
    
    # Set the index directory dynamically
    index_dir = os.path.join(current_dir, index_name)
    index_path = os.path.join(index_dir, "index")
    
    print(f"Checking for index at: {index_path}/index.faiss")
    if os.path.exists(f"{index_path}/index.faiss"):
        print(f"Loading existing vector store from {index_path}/index.faiss")
        try:
            vectorstore = FAISS.load_local(index_path, OpenAIEmbeddings(), allow_dangerous_deserialization=True)
            print("Vector store loaded successfully.")
            return vectorstore
        except Exception as e:
            print(f"Error loading vector store: {e}")
            print("Will create a new vector store.")
    else:
        print(f"Vector store not found at {index_path}/index.faiss")
    
    print("Creating new vector store...")
    docs = load_documents(sources)
    text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=100, chunk_overlap=50
    )
    doc_splits = text_splitter.split_documents(docs)
    vectorstore = FAISS.from_documents(doc_splits, OpenAIEmbeddings())
    
    print(f"Saving vector store to {index_path}...")
    try:
        os.makedirs(index_dir, exist_ok=True)  # Ensure the directory exists
        vectorstore.save_local(index_path)
        print("Vector store saved successfully.")
    except Exception as e:
        print(f"Error saving vector store: {e}")
    
    return vectorstore

# List of URLs and PDF files to load documents from
sources = [
    "./index_vsa/vsa.pdf"
]

# Create or load vector store
vectorstore = create_or_load_vectorstore(sources)

# Create retriever
retriever = vectorstore.as_retriever(k=10)

# Initialize web search tool
web_search_tool = TavilySearchResults()

rag_prompt = PromptTemplate(
    template="""
    You are a Vessel Sharing Agreement (VSA) specialist with a deep understanding of maritime logistics and shipping contracts. Your task is to provide detailed and accurate information in response to user questions about VSAs, particularly focusing on the agreement between CHERRY and OLIVE shipping liners.

    Guidelines for answering:
    1. Use the provided context (Documents) to answer the user's question in detail.
    2. Create a final answer with references, using "SOURCE[number]" in capital letters (e.g., SOURCE[1], SOURCE[2]).
    3. Present information in a clear, concise, and easily understandable manner, using bullet points for organization when appropriate.
    4. If the question is unclear, politely ask the user for clarification.
    5. For questions about specific VSA terms:
       - Provide details on: definition, relevant clauses, obligations of parties, penalties or compensations (if applicable), and any related operational procedures.
       - If this information is not in the provided context, state that you need to refer to the full VSA document for accurate details.
    6. Respond in the language of the user's question. If unable to determine the language, default to English.
    7. If you don't know the answer or if the information is not in the provided context, clearly state "I don't have enough information to answer this question accurately. Please refer to the full Vessel Sharing Agreement document or consult with a VSA expert for the most up-to-date and accurate information."
    8. Limit your response to a maximum of 300 words unless the question specifically requires a longer answer.

    Context format:
    The 'Documents' field contains relevant excerpts from the Vessel Sharing Agreement and related sources. Each document is separated by triple dashes (---) and may include metadata such as section titles or clause numbers.

    Question: {question}
    Documents: {documents}
    Answer:
    """,
    input_variables=["question", "documents"],
)

rag_chain = rag_prompt | llm | StrOutputParser()

# Data model for the output
class GradeDocuments(BaseModel):
    """Binary score for relevance check on retrieved documents."""

    binary_score: str = Field(
        description="Documents are relevant to the question, 'yes' or 'no'"
    )

# LLM with tool call
structured_llm_grader = llm.with_structured_output(GradeDocuments)

# Prompt
system = """You are a teacher grading a quiz. You will be given: 
1/ a QUESTION 
2/ a set of comma separated FACTS provided by the student

You are grading RELEVANCE RECALL:
A score of 1 means that ANY of the FACTS are relevant to the QUESTION. 
A score of 0 means that NONE of the FACTS are relevant to the QUESTION. 
1 is the highest (best) score. 0 is the lowest score you can give. 

Explain your reasoning in a step-by-step manner. Ensure your reasoning and conclusion are correct. 

Avoid simply stating the correct answer at the outset."""

grade_prompt = ChatPromptTemplate.from_messages(
    [
        ("system", system),
        ("human", "FACTS: \n\n {documents} \n\n QUESTION: {question}"),
    ]
)

retrieval_grader = grade_prompt | structured_llm_grader


class GraphState(TypedDict):
    """
    Represents the state of our graph.

    Attributes:
        question: question
        generation: LLM generation
        search: whether to add search
        documents: list of documents
    """

    question: str
    generation: str
    search: str
    documents: List[str]
    steps: List[str]
    answer: str


def retrieve(state):
    """
    Retrieve documents

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): New key added to state, documents, that contains retrieved documents
    """
    question = state["question"]
    documents = retriever.invoke(question)
    if state["steps"]:
        steps = state["steps"]
    else:
        steps = []
    steps.append("retrieve_documents")
    return {"documents": documents, "question": question, "steps": steps}


def generate(state):
    """
    Generate answer

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): New key added to state, generation, that contains LLM generation
    """

    question = state["question"]
    documents = state["documents"]
    generation = rag_chain.invoke({"documents": documents, "question": question})
    steps = state["steps"]
    steps.append("generate_answer")
    return {
        "documents": documents,
        "question": question,
        "generation": generation,
        "steps": steps,
    }


def grade_documents(state):
    """
    Determines whether the retrieved documents are relevant to the question.

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): Updates documents key with only filtered relevant documents
    """

    question = state["question"]
    documents = state["documents"]
    steps = state["steps"]
    steps.append("grade_document_retrieval")
    filtered_docs = []
    search = "No"
    for d in documents:
        score = retrieval_grader.invoke(
            {"question": question, "documents": d.page_content}
        )
        grade = score.binary_score
        if grade == "yes":
            filtered_docs.append(d)
        else:
            search = "Yes"
            continue
    return {
        "documents": filtered_docs,
        "question": question,
        "search": search,
        "steps": steps,
    }


def web_search(state):
    """
    Web search based on the re-phrased question.

    Args:
        state (dict): The current graph state

    Returns:
        state (dict): Updates documents key with appended web results
    """

    question = state["question"]
    documents = state.get("documents", [])
    steps = state["steps"]
    steps.append("web_search")
    web_results = web_search_tool.invoke({"query": question})
    documents.extend(
        [
            Document(page_content=d["content"], metadata={"url": d["url"]})
            for d in web_results
        ]
    )
    return {"documents": documents, "question": question, "steps": steps}


def decide_to_generate(state):
    """
    Determines whether to generate an answer, or re-generate a question.

    Args:
        state (dict): The current graph state

    Returns:
        str: Binary decision for next node to call
    """
    search = state["search"]
    if search == "Yes":
        return "search"
    else:
        return "generate"


# Graph
workflow = StateGraph(GraphState)

# Define the nodes
workflow.add_node("retrieve", retrieve)  # retrieve
workflow.add_node("grade_documents", grade_documents)  # grade documents
workflow.add_node("generate", generate)  # generatae
workflow.add_node("web_search", web_search)  # web search

# Build graph
workflow.set_entry_point("retrieve")
workflow.add_edge("retrieve", "grade_documents")
workflow.add_conditional_edges(
    "grade_documents",
    decide_to_generate,
    {
        "search": "web_search",
        "generate": "generate",
    },
)
workflow.add_edge("web_search", "generate")
workflow.add_edge("generate", END)

app = workflow.compile()


# Function for processing streamlit questions
def run_streamlit():
        # Initialize session state for VSA messages
    if "schedule_messages" not in st.session_state:
        st.session_state.schedule_messages = []

    # Display chat messages
    for message in st.session_state.schedule_messages:
        with st.chat_message(message["role"]):
            st.markdown(f"{message['content']}\n\n<div style='font-size:0.8em; color:#888;'>{message['timestamp']}</div>", unsafe_allow_html=True)
            if "steps" in message and message["role"] == "assistant":
                with st.expander("View steps"):
                    st.write(message["steps"])

    # Chat input
    prompt = st.chat_input("Any question about VSA?")

    if prompt:
        # Add user message
        user_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st.session_state.schedule_messages.append({"role": "user", "content": prompt, "timestamp": user_timestamp})
        
        # Display user message
        with st.chat_message("user"):
            st.markdown(f"{prompt}\n\n<div style='font-size:0.8em; color:#888;'>{user_timestamp}</div>", unsafe_allow_html=True)
        
        # Get AI response
        with st.spinner("Thinking..."):
            try:
                config = {"configurable": {"thread_id": str(uuid.uuid4())}}
                response = app.invoke(
                    {
                        "question": prompt,
                        "generation": "",
                        "search": "",
                        "documents": [],
                        "steps": []
                    },
                    config
                )
                ai_response = response.get("generation", "No response generated")
                ai_steps = response.get("steps", [])
                ai_timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
                # Add and display AI response
                st.session_state.schedule_messages.append({"role": "assistant", "content": ai_response, "timestamp": ai_timestamp, "steps": ai_steps})
                with st.chat_message("assistant"):
                    st.markdown(f"{ai_response}\n\n<div style='font-size:0.8em; color:#888;'>{ai_timestamp}</div>", unsafe_allow_html=True)
                    with st.expander("View steps"):
                        st.write(ai_steps)
            except Exception as e:
                st.error(f"An error occurred: {e}")
        
        st.rerun()

# Run the main functions
def run():
    return vsa_validator

# Run the streamlit function
if __name__ == "__main__":
    run_streamlit()