import ast
import inspect
from dataclasses import dataclass
from datetime import datetime
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    NamedTuple,
    Optional,
    Self,
    Set,
    Tuple,
    Union,
)

from pydantic import BaseModel

from tiny_graph.constants import END, START


@dataclass(frozen=True)
class Edge:
    start_node: str
    end_node: str
    id: Optional[str]

    def __hash__(self):
        return hash((self.start_node, self.end_node))


class Node(NamedTuple):
    name: str
    action: Callable[..., None]
    metadata: Optional[Dict[str, Any]] = None
    is_async: bool = False
    is_router: bool = False
    possible_routes: Optional[Set[str]] = None
    interrupt: Union[Literal["before", "after"], None] = None
    emit_event: Optional[Callable] = None


class BaseGraph:
    def __init__(self, state: Union[BaseModel, NamedTuple, None] = None):
        self.nodes: Dict[str, Node] = {}
        self.nodes[START] = Node(START, lambda: None, None, False, False, None)
        self.nodes[END] = Node(END, lambda: None, None, False, False, None)
        self.edges: Set[Edge] = set()
        self.is_compiled: bool = False
        self.tasks: List[Callable[..., None]] = []
        self.event_handlers: List[Callable] = []

        # Validate state type
        if state is not None and not isinstance(state, (BaseModel, NamedTuple)):
            raise TypeError(
                "State must be either a Pydantic BaseModel, NamedTuple, or None"
            )
        self.state: Union[BaseModel, NamedTuple, None] = state
        self._has_state = state is not None

        self.edge_counter: Dict[
            Tuple[str, str], int
        ] = {}  # Track edge counts between node pairs

    @property
    def _all_nodes(self) -> List[str]:
        return list(self.nodes.keys())

    @property
    def _all_edges(self) -> Set[Tuple[str, str]]:
        return self.edges

    def _get_return_values(self, func: Callable) -> Set[str]:
        """Extract all string literal return values from a function"""
        source = inspect.getsource(func)
        # Dedent the source code before parsing
        source = inspect.cleandoc(source)
        tree = ast.parse(source)

        return_values = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Return):
                # Handle direct string returns
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ):
                    return_values.add(node.value.value)
                # Handle string variable returns
                elif isinstance(node.value, ast.Name):
                    # Note: This is a limitation - we can only detect direct string returns
                    # Variable returns would require more complex static analysis
                    pass
        return return_values

    def _force_compile(self):
        if not self.is_compiled:
            print("Graph not compiled. Compiling now..")
            self.compile()

    def node(
        self,
        name: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        interrupt: Union[Literal["before", "after"], None] = None,
    ):
        """Decorator to add a node to the graph

        Args:
            name: Optional name for the node. If None, uses the function name
            metadata: Optional metadata dictionary
        """

        def decorator(func: Callable[..., None]):
            if self.is_compiled:
                raise ValueError("Cannot add nodes after compiling the graph")

            # Checking for reserved node names
            node_name = name if name is not None else func.__name__
            if node_name in [START, END]:
                raise ValueError(f"Node name '{node_name}' is reserved")

            # Checking for async calls
            is_async = inspect.iscoroutinefunction(func)

            # adding metadata to the node
            func.__metadata__ = {
                "interrupt": interrupt,
            }

            # Check if this is a router node by looking for return statements
            return_values = self._get_return_values(func)
            is_router = len(return_values) > 0

            # Check if function accepts state parameter when graph has state
            if hasattr(self, "_has_state") and self._has_state:
                sig = inspect.signature(func)
                if "state" not in sig.parameters:
                    raise ValueError(
                        f"Node function '{func.__name__}' must accept 'state' parameter when graph has state. "
                        f"Update your function definition to: def {func.__name__}(state) -> Dict"
                    )

            # Create event emitter closure
            async def emit_event(event_type: str, data: Any = None):
                if hasattr(self, "event_handlers"):
                    event = {
                        "type": "node_event",
                        "event_type": event_type,
                        "node_id": node_name,
                        "chain_id": getattr(self, "chain_id", None),
                        "timestamp": datetime.now(),
                        "data": data,
                    }
                    for handler in self.event_handlers:
                        await handler(event)

            # Attach emit_event to the function
            func.emit_event = emit_event

            self.nodes[node_name] = Node(
                node_name,
                func,
                metadata,
                is_async,
                is_router,
                return_values if is_router else None,
                interrupt,
                emit_event=emit_event,
            )
            return func

        return decorator

    def add_edge(
        self, start_node: str, end_node: str, id: Optional[str] = None
    ) -> Self:
        if start_node not in self.nodes or end_node not in self.nodes:
            raise ValueError(
                f"Both start_node '{start_node}' and end_node '{end_node}' must be added to the graph before adding an edge"
            )

        # Auto-generate edge ID if not provided
        if id is None:
            node_pair = (start_node, end_node)
            self.edge_counter[node_pair] = self.edge_counter.get(node_pair, 0) + 1
            id = f"{start_node}_to_{end_node}_{self.edge_counter[node_pair]}"

        self.edges.add(Edge(id=id, start_node=start_node, end_node=end_node))
        return self

    def validate(self) -> Self:
        """Validate that the graph starts with '__start__', ends with '__end__', and all nodes are on valid paths.

        Raises:
            ValueError: If start/end nodes are missing, orphaned nodes are found, dead ends exist, or if there are cycles
        """
        # Check for required start and end nodes
        if START not in self.nodes:
            raise ValueError("Graph must have a '__start__' node")
        if END not in self.nodes:
            raise ValueError("Graph must have an '__end__' node")

        # Track visited nodes starting from __start__
        visited = set()
        stack = set()  # For cycle detection
        self._dfs_validate(START, visited, stack)

        # Check if __end__ is reachable
        if END not in visited:
            raise ValueError("'__end__' node is not reachable from '__start__'")

        # Check for orphaned nodes (nodes not reachable from __start__)
        orphaned_nodes = set(self.nodes.keys()) - visited
        if orphaned_nodes:
            raise ValueError(
                f"Found orphaned nodes not reachable from '__start__': {orphaned_nodes}"
            )

        # Check for graphs with only one node added or less
        if len(self.nodes) <= 3:
            raise ValueError("Graph with only one node is not valid")

        # Check for dead ends (nodes with no outgoing edges, except END)
        nodes_with_outgoing_edges = {edge.start_node for edge in self.edges}
        dead_ends = set(self.nodes.keys()) - nodes_with_outgoing_edges - {END}
        if dead_ends:
            raise ValueError(
                f"Found dead-end nodes with no outgoing edges: {dead_ends}"
            )

        # Validate router nodes
        for node_name, node in self.nodes.items():
            if node.is_router and node.possible_routes:
                # Check if all possible routes exist as nodes
                invalid_routes = node.possible_routes - set(self.nodes.keys())
                if invalid_routes:
                    raise ValueError(
                        f"Router node '{node_name}' contains invalid routes: {invalid_routes}"
                    )

                # Check if all edges from this router point to declared possible routes
                router_edges = {
                    edge.end_node for edge in self.edges if edge.start_node == node_name
                }
                invalid_edges = router_edges - set(node.possible_routes)
                if invalid_edges:
                    raise ValueError(
                        f"Router node '{node_name}' has edges to undeclared routes: {invalid_edges}"
                    )

        return True

    def _dfs_validate(self, node: str, visited: Set[str], stack: Set[str]) -> None:
        """Helper method for depth-first search validation.

        Args:
            node: Current node being visited
            visited: Set of nodes visited in this DFS traversal
            stack: Set of nodes in the current DFS stack (for cycle detection)

        Raises:
            ValueError: If a cycle is detected in the graph
        """
        if node in stack:
            raise ValueError(f"Cycle detected in graph involving node: {node}")

        if node in visited:
            return

        visited.add(node)
        stack.add(node)

        # Get all edges starting from current node
        outgoing_edges = {
            edge.end_node for edge in self.edges if edge.start_node == node
        }

        for next_node in outgoing_edges:
            self._dfs_validate(next_node, visited, stack)

        stack.remove(node)

    def _find_execution_paths(self) -> List[Any]:
        """Identifies execution paths in the graph with parallel and nested parallel paths."""

        def get_next_nodes(node: str) -> List[str]:
            return [edge.end_node for edge in self.edges if edge.start_node == node]

        def get_prev_nodes(node: str) -> List[str]:
            return [edge.start_node for edge in self.edges if edge.end_node == node]

        def is_convergence_point(node: str) -> bool:
            return len(get_prev_nodes(node)) > 1

        def find_convergence_point(start_nodes: List[str]) -> str:
            """Find where multiple paths converge."""
            visited_by_path = {start: set([start]) for start in start_nodes}
            current_nodes = {start: [start] for start in start_nodes}

            while True:
                # For each path, get the next node
                for start in start_nodes:
                    if current_nodes[start][-1] != END:
                        next_node = get_next_nodes(current_nodes[start][-1])[0]
                        visited_by_path[start].add(next_node)
                        current_nodes[start].append(next_node)

                # Check if any node is visited by multiple paths
                all_visited = set()
                for nodes in visited_by_path.values():
                    intersection = all_visited.intersection(nodes)
                    if intersection:
                        return list(intersection)[0]
                    all_visited.update(nodes)

        def build_path_to_convergence(
            start: str, convergence: str, visited: Set[str], parent: str
        ) -> List[Any]:
            if start in visited:
                return []

            path = []
            current = start
            visited.add(current)

            while current != convergence:
                next_nodes = get_next_nodes(current)
                if len(next_nodes) > 1:
                    # Handle nested parallel paths
                    nested_convergence = find_convergence_point(next_nodes)
                    nested_paths = []
                    for next_node in next_nodes:
                        nested_path = build_path_to_convergence(
                            next_node, nested_convergence, visited.copy(), current
                        )
                        if nested_path:
                            nested_paths.append(nested_path)
                    path.extend(
                        [(current, current), nested_paths]
                    )  # Include parent info
                    current = nested_convergence
                elif not next_nodes:
                    path.append((parent, current))
                    visited.add(current)
                    break
                elif is_convergence_point(current):
                    if "nested_convergence" in locals():
                        path.append((parent, current))
                        current = next_nodes[0]
                        visited.add(current)
                    else:
                        break
                else:
                    path.append((parent, current))
                    current = next_nodes[0]
                    parent = current  # Update parent for next iteration
                    visited.add(current)

            return path

        def build_execution_plan(current: str, parent: str = START) -> List[Any]:
            if current == END:
                return []

            next_nodes = get_next_nodes(current)

            # Skip START node
            if current == START and len(next_nodes) == 1:
                return build_execution_plan(next_nodes[0], current)

            # Handle parallel paths
            if len(next_nodes) > 1:
                convergence = find_convergence_point(next_nodes)
                parallel_paths = []

                for next_node in next_nodes:
                    path = build_path_to_convergence(
                        next_node, convergence, set(), current
                    )
                    if isinstance(path, list) and len(path) == 1:
                        path = path[0]
                    parallel_paths.append(path)

                return [(parent, current), parallel_paths] + build_execution_plan(
                    convergence, current
                )

            # Handle sequential path
            return [(parent, current)] + build_execution_plan(next_nodes[0], current)

        # Build and clean the execution plan
        plan = build_execution_plan(START)

        def clean_plan(p):
            # Handle non-list items (like tuples)
            if not isinstance(p, list):
                return p

            # Handle empty lists
            if not p:
                return p

            # Handle single-item lists
            if len(p) == 1:
                return clean_plan(p[0])

            # Clean the list items
            return [
                clean_plan(item)
                for item in p
                if item is not None
                and (
                    not isinstance(item, tuple)  # Handle non-tuple items
                    or (len(item) == 2 and item[1] != START)  # Handle tuples
                )
            ]

        return clean_plan(plan)

    def _find_execution_paths_with_edges(self) -> List[Any]:
        """Similar to _find_execution_paths but returns edge IDs instead of node names."""

        def find_all_edges_with_node(node_name):
            edges = [edge.id for edge in self.edges if edge.end_node == node_name]
            if not edges:
                raise ValueError(f"No edges found for node: {node_name}")
            return edges if len(edges) > 1 else edges[0]

        def scan_execution_plan(execution_plan):
            final_edge_plan = []
            for step in execution_plan:
                if isinstance(step, list):
                    nested_result = [
                        scan_execution_plan(node)
                        if isinstance(node, list)
                        else find_all_edges_with_node(node)
                        for node in step
                    ]

                    final_edge_plan.append(nested_result)
                else:
                    if step != START:
                        result = find_all_edges_with_node(step)
                        final_edge_plan.append(result)

            return final_edge_plan

        return scan_execution_plan(self.execution_path)

    def compile(self, state: Union[BaseModel, NamedTuple, None] = None) -> Self:
        """Compiles the graph by validating and organizing execution paths."""
        self.validate()
        self.detailed_execution_path = self._find_execution_paths()

        def extract_execution_plan(current_item):
            if isinstance(current_item, list):
                return [extract_execution_plan(item) for item in current_item]

            elif isinstance(current_item, str):
                return current_item
            else:
                return current_item[1]

        self.execution_path = [
            extract_execution_plan(item) for item in self.detailed_execution_path
        ]
        self.execution_plan_with_edges = self._find_execution_paths_with_edges()

        self.is_compiled = True
        return self

    def visualize(self, output_file: str = "graph") -> None:
        """
        Visualize the graph using Graphviz.

        Args:
            output_file: Name of the output file (without extension)
        """
        from graphviz import Digraph

        self._force_compile()

        dot = Digraph(comment="Graph Visualization", format="svg")
        dot.attr(rankdir="LR")  # Left to right layout

        # Set global graph attributes for better typography
        dot.attr("node", fontname="Helvetica", fontsize="10", margin="0.2,0.1")
        dot.attr("edge", fontname="Helvetica", fontsize="9")

        # Add nodes with styling
        for node_name, node in self.nodes.items():
            # Create label with metadata if it exists
            label = node_name
            if node.metadata or node.is_async or node.is_router:
                metadata_dict = node.metadata or {}
                if node.is_router:
                    metadata_dict = {**metadata_dict, "router": "true"}
                    if node.possible_routes:
                        metadata_dict["routes"] = ",".join(node.possible_routes)
                metadata_str = "\n".join(f"{k}: {v}" for k, v in metadata_dict.items())
                label = f"{node_name}\n{metadata_str}"

            # Define node styles based on type
            node_attrs = {
                "style": "rounded,filled",
                "fillcolor": "lightblue",
                "shape": "box",
                "height": "0.4",
                "width": "0.6",
                "margin": "0.15",
            }

            # Special styling for different node types
            if node_name in [START, END]:
                node_attrs.update(
                    {
                        "shape": "ellipse",
                        "style": "filled",
                        "height": "0.3",
                        "width": "0.5",
                        "penwidth": "1",
                    }
                )
                # Different colors for START and END

                node_attrs["fillcolor"] = "#F4E8E8"  # Pale rose

            elif node.is_router:
                node_attrs.update(
                    {"shape": "diamond", "fillcolor": "lightyellow", "height": "0.5"}
                )
            elif node.is_async:
                label = f"<{node_name}<BR/><I><FONT POINT-SIZE='8'>async</FONT></I>>"
                node_attrs.update(
                    {
                        "shape": "box",
                        "fillcolor": "#E0FFFF",  # Light cyan
                        "style": "rounded,filled",
                        "fontname": "Helvetica",
                        "margin": "0.15",
                        "height": "0.5",
                    }
                )

            # Use HTML-like label for async nodes, regular text for others
            if node.is_async:
                dot.node(node_name, label, **node_attrs)
            else:
                dot.node(node_name, node_name, **node_attrs)

        # Add edges with refined styling
        for edge in self.edges:
            dot.edge(edge.start_node, edge.end_node, penwidth="0.8", arrowsize="0.7")

        # Return the SVG source directly instead of saving to file
        return dot

    def find_edges(
        self,
        start_node: Optional[str] = None,
        end_node: Optional[str] = None,
        edge_id: Optional[str] = None,
    ) -> Set[Edge]:
        """Find edges matching the given criteria"""
        result = self.edges

        if edge_id:
            result = {edge for edge in result if edge.id == edge_id}
        if start_node:
            result = {edge for edge in result if edge.start_node == start_node}
        if end_node:
            result = {edge for edge in result if edge.end_node == end_node}

        return result
